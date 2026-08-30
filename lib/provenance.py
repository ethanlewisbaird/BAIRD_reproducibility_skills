#!/usr/bin/env python3
"""provenance.py — deterministic provenance runtime for agentic bioinformatics.

Self-contained (Python 3 stdlib only, zero dependencies). Works locally or over SSH.

Subcommands:
  init     Start a run: create results/run_<ts>_<id>/, capture git state, env exports, R sessionInfo.
  exec     Wrap one execution: hash inputs, run command, hash outputs, record metadata + logs.
  verify   Check recorded artifacts still exist and match hashes; optionally re-run and compare.
  report   Emit a Markdown reproducibility appendix (figure -> script -> env -> data -> seed).

Verdicts:
  PROVENANCE_COMPLETE     all recorded artifacts intact + git/env records present
  PROVENANCE_INCOMPLETE   something missing or hash mismatch
  REPRODUCTION_VERIFIED   re-run produced identical output hashes
  REPRODUCTION_DIFFERS    re-run produced different output hashes (or failed)

Design rules (from design review):
  - Path = location, SHA-256 = identity. Never trust paths alone.
  - The provenance graph is the index; large artifacts stay in place on disk.
  - The event log (provenance.jsonl) is append-only source of truth; manifest.json is a materialized summary.
  - Failed runs are recorded, not discarded.
  - Secrets are never recorded.
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import secrets
import subprocess
import sys
import time

SCHEMA = "baird-provenance/v1"
CHUNK = 1 << 20  # 1 MiB
RESULTS_DIR = "results"
RUN_DIR_FMT = "run_%Y%m%d_%H%M%S"


def now_iso() -> str:
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def new_run_id() -> str:
    ts = datetime.datetime.now().strftime(RUN_DIR_FMT)
    return f"{ts}_{secrets.token_hex(4)}"


# --------------------------------------------------------------------------- hashing

def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            block = f.read(CHUNK)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def sha256_dir(path: str) -> str:
    """Deterministic recursive dir hash: sorted relative paths + per-file sha256."""
    h = hashlib.sha256()
    for root, dirs, files in os.walk(path):
        dirs.sort()
        files.sort()
        for fn in files:
            fp = os.path.join(root, fn)
            rel = os.path.relpath(fp, path)
            h.update(rel.encode("utf-8"))
            h.update(b"\x00")
            h.update(sha256_file(fp).encode("utf-8"))
            h.update(b"\x00")
    return h.hexdigest()


def sha256_path(path: str) -> str:
    if os.path.isdir(path):
        return sha256_dir(path)
    return sha256_file(path)


def artifact_id(path: str) -> str:
    return f"sha256:{sha256_path(path)}"


# --------------------------------------------------------------------------- run store

def find_run(run_arg: str) -> str | None:
    """Accept a run name (run_...) or a path. Searches cwd/results and cwd."""
    if os.path.isdir(run_arg):
        return os.path.abspath(run_arg)
    for base in (os.path.join(os.getcwd(), RESULTS_DIR), os.getcwd()):
        cand = os.path.join(base, run_arg)
        if os.path.isdir(cand):
            return cand
    return None


def _run_dir(run_arg: str) -> str:
    d = find_run(run_arg)
    if not d:
        sys.exit(f"ERROR: run not found: {run_arg}")
    return d


def manifest_path(run_dir: str) -> str:
    return os.path.join(run_dir, "manifest.json")


def events_path(run_dir: str) -> str:
    return os.path.join(run_dir, "provenance.jsonl")


def load_manifest(run_dir: str) -> dict:
    p = manifest_path(run_dir)
    if os.path.exists(p):
        with open(p) as f:
            return json.load(f)
    return {}


def save_manifest(run_dir: str, manifest: dict) -> None:
    with open(manifest_path(run_dir), "w") as f:
        json.dump(manifest, f, indent=2)
        f.write("\n")


def append_event(run_dir: str, event: dict) -> None:
    event.setdefault("timestamp", now_iso())
    with open(events_path(run_dir), "a") as f:
        f.write(json.dumps(event) + "\n")


def load_events(run_dir: str) -> list[dict]:
    evs = []
    p = events_path(run_dir)
    if os.path.exists(p):
        with open(p) as f:
            for line in f:
                line = line.strip()
                if line:
                    evs.append(json.loads(line))
    return evs


def run_cmd(cmd: str, timeout: int = 600, cwd: str | None = None) -> tuple[int, str, str]:
    try:
        p = subprocess.run(
            cmd, shell=True, capture_output=True, text=True,
            timeout=timeout, cwd=cwd,
        )
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"
    except Exception as e:  # noqa: BLE001
        return -1, "", str(e)


# --------------------------------------------------------------------------- captures

def git_state(cwd: str | None = None) -> dict | None:
    """Best-effort git capture: commit, branch, dirty-tree hash (of `git status --porcelain`)."""
    rc, head, _ = run_cmd("git rev-parse HEAD 2>/dev/null", cwd=cwd)
    if rc != 0 or not head.strip():
        return None
    _, branch, _ = run_cmd("git rev-parse --abbrev-ref HEAD 2>/dev/null", cwd=cwd)
    rc2, dirty, _ = run_cmd("git status --porcelain 2>/dev/null", cwd=cwd)
    dirty_hash = hashlib.sha256(dirty.encode("utf-8")).hexdigest() if rc2 == 0 else "unknown"
    return {
        "commit": head.strip(),
        "branch": branch.strip() or "unknown",
        "dirty_files": len([l for l in dirty.splitlines() if l.strip()]) if rc2 == 0 else -1,
        "dirty_tree_hash": dirty_hash,
    }


def capture_conda_env(env_name: str, out_dir: str) -> dict:
    """Best-effort `conda env export` (full pin). Returns record or error."""
    out = os.path.join(out_dir, f"env_{env_name}.yml")
    for mgr in ("conda", "micromamba"):
        rc, stdout, _ = run_cmd(f"{mgr} env export -n {env_name} 2>/dev/null", timeout=300)
        if rc == 0 and stdout.strip():
            with open(out, "w") as f:
                f.write(stdout)
            return {"name": env_name, "manager": mgr, "file": os.path.basename(out),
                    "sha256": sha256_file(out)}
    return {"name": env_name, "error": "conda/micromamba env export failed"}


def capture_r_session(rscript: str, out_dir: str) -> dict:
    out = os.path.join(out_dir, "sessionInfo.txt")
    rc, stdout, _ = run_cmd(f"{rscript} -e 'sessionInfo()' 2>/dev/null", timeout=300)
    if rc == 0 and stdout.strip():
        with open(out, "w") as f:
            f.write(stdout)
        return {"rscript": rscript, "file": os.path.basename(out), "sha256": sha256_file(out)}
    return {"rscript": rscript, "error": "R sessionInfo capture failed"}


# --------------------------------------------------------------------------- init

def cmd_init(args: argparse.Namespace) -> int:
    project = os.path.abspath(args.project or os.getcwd())
    results_root = os.path.join(project, RESULTS_DIR)
    os.makedirs(results_root, exist_ok=True)

    run_dir = os.path.join(results_root, new_run_id())
    for sub in ("environment", "executions", "logs", "code", "outputs"):
        os.makedirs(os.path.join(run_dir, sub), exist_ok=True)

    manifest = {
        "schema": SCHEMA,
        "run_id": os.path.basename(run_dir),
        "created_at": now_iso(),
        "host": os.uname().nodename if hasattr(os, "uname") else "unknown",
        "user": os.environ.get("USER", "unknown"),
        "project": project,
        "research_question": args.question or "",
        "seed": args.seed,
        "git": git_state(project),
        "environment": {"conda_envs": [], "r_session": None},
        "executions": [],
        "artifacts": {},
        "verdicts": {},
    }

    for env in args.env or []:
        rec = capture_conda_env(env, os.path.join(run_dir, "environment"))
        manifest["environment"]["conda_envs"].append(rec)

    if args.rscript:
        manifest["environment"]["r_session"] = capture_r_session(args.rscript, os.path.join(run_dir, "environment"))

    save_manifest(run_dir, manifest)
    append_event(run_dir, {"event": "run_started", "run_id": manifest["run_id"], "seed": args.seed})

    print(run_dir)
    return 0


# --------------------------------------------------------------------------- exec

def _hash_path_list(paths: str | None) -> list[dict]:
    out = []
    for p in (paths or "").split(","):
        p = p.strip()
        if not p:
            continue
        ap = os.path.abspath(p)
        if not os.path.exists(ap):
            out.append({"path": p, "error": "missing at record time"})
        else:
            rec = {"path": p, "sha256": sha256_path(ap)}
            if os.path.isfile(ap):
                rec["size_bytes"] = os.path.getsize(ap)
            out.append(rec)
    return out


def cmd_exec(args: argparse.Namespace) -> int:
    run_dir = _run_dir(args.run)
    manifest = load_manifest(run_dir)
    run_id = manifest.get("run_id", os.path.basename(run_dir))

    n = len(manifest.get("executions", []))
    exec_id = f"exec_{n + 1:03d}"

    inputs = _hash_path_list(args.inputs)

    # Copy the script into code/ if requested (only for scripts NOT in git).
    code_ref = None
    if args.copy:
        src = os.path.abspath(args.copy)
        if os.path.isfile(src):
            dest = os.path.join(run_dir, "code", os.path.basename(src))
            if os.path.abspath(dest) != src:
                with open(src, "rb") as fi, open(dest, "wb") as fo:
                    while True:
                        blk = fi.read(CHUNK)
                        if not blk:
                            break
                        fo.write(blk)
            code_ref = {"path": os.path.join("code", os.path.basename(src)),
                        "sha256": sha256_file(dest)}

    append_event(run_dir, {"event": "execution_started", "execution_id": exec_id,
                           "command": args.cmd, "env": args.env, "seed": args.seed})

    t0 = time.time()
    rc, stdout, stderr = run_cmd(args.cmd, timeout=args.timeout, cwd=args.cwd)
    duration = round(time.time() - t0, 2)

    # Hash outputs AFTER execution so we record the produced files.
    outputs = _hash_path_list(args.outputs)

    log_base = os.path.join(run_dir, "logs", exec_id)
    with open(log_base + ".stdout.log", "w") as f:
        f.write(stdout or "")
    with open(log_base + ".stderr.log", "w") as f:
        f.write(stderr or "")

    rec = {
        "execution_id": exec_id,
        "name": args.name or exec_id,
        "command": args.cmd,
        "cwd": os.path.abspath(args.cwd) if args.cwd else os.getcwd(),
        "env": args.env,
        "seed": args.seed,
        "started_at": now_iso(),
        "duration_s": duration,
        "exit_code": rc,
        "code": code_ref,
        "inputs": inputs,
        "outputs": outputs,
        "stdout_log": f"logs/{exec_id}.stdout.log",
        "stderr_log": f"logs/{exec_id}.stderr.log",
    }
    manifest.setdefault("executions", []).append(rec)

    for a in inputs + outputs:
        if "sha256" in a:
            manifest.setdefault("artifacts", {})[a["sha256"]] = {
                "path": a["path"], "size_bytes": a.get("size_bytes"),
            }

    save_manifest(run_dir, manifest)
    append_event(run_dir, {"event": "execution_finished", "execution_id": exec_id,
                           "exit_code": rc, "duration_s": duration})

    print(json.dumps({"execution_id": exec_id, "exit_code": rc,
                      "outputs": [o.get("sha256") for o in outputs]}, indent=2))
    return 0


# --------------------------------------------------------------------------- verify

def verify_integrity(manifest: dict) -> tuple[str, list[dict]]:
    checks = []
    ok = True
    for aid, meta in manifest.get("artifacts", {}).items():
        p = meta.get("path")
        if not p or not os.path.exists(p):
            checks.append({"artifact": aid, "path": p, "status": "MISSING"})
            ok = False
            continue
        cur = sha256_path(p)
        if cur == aid:
            checks.append({"artifact": aid, "path": p, "status": "OK"})
        else:
            checks.append({"artifact": aid, "path": p, "status": "HASH_MISMATCH"})
            ok = False
    env_records = manifest.get("environment", {})
    env_ok = bool(env_records.get("conda_envs")) or bool(env_records.get("r_session"))
    git_ok = bool(manifest.get("git"))
    verdict = "PROVENANCE_COMPLETE" if (ok and env_ok and git_ok) else "PROVENANCE_INCOMPLETE"
    return verdict, checks


def verify_reproduction(manifest: dict, timeout: int) -> tuple[str, list[dict]]:
    details = []
    all_match = True
    for rec in manifest.get("executions", []):
        rc, stdout, stderr = run_cmd(rec["command"], timeout=timeout, cwd=rec.get("cwd"))
        out_match = True
        for o in rec.get("outputs", []):
            if "sha256" not in o or "error" in o:
                continue
            if not os.path.exists(o["path"]):
                out_match = False
                continue
            if sha256_path(o["path"]) != o["sha256"]:
                out_match = False
        match = (rc == rec.get("exit_code")) and out_match
        if not match:
            all_match = False
        details.append({"execution_id": rec["execution_id"], "rerun_exit": rc,
                        "recorded_exit": rec.get("exit_code"), "outputs_match": out_match,
                        "match": match})
    verdict = "REPRODUCTION_VERIFIED" if all_match else "REPRODUCTION_DIFFERS"
    return verdict, details


def cmd_verify(args: argparse.Namespace) -> int:
    run_dir = _run_dir(args.run)
    manifest = load_manifest(run_dir)

    verdict, checks = verify_integrity(manifest)
    manifest["verdicts"]["integrity"] = verdict
    manifest["verdicts"]["integrity_checks"] = checks
    append_event(run_dir, {"event": "verify_integrity", "verdict": verdict})

    print(f"integrity: {verdict}")
    for c in checks:
        print(f"  {c['status']:<14} {c['path']}")

    if args.rerun:
        rv, details = verify_reproduction(manifest, args.timeout)
        manifest["verdicts"]["reproduction"] = rv
        manifest["verdicts"]["reproduction_details"] = details
        append_event(run_dir, {"event": "verify_reproduction", "verdict": rv})
        print(f"reproduction: {rv}")
        for d in details:
            print(f"  {d['execution_id']}: rerun_exit={d['rerun_exit']} "
                  f"recorded_exit={d['recorded_exit']} outputs_match={d['outputs_match']}")

    save_manifest(run_dir, manifest)
    return 0 if verdict == "PROVENANCE_COMPLETE" else 1


# --------------------------------------------------------------------------- report

def _figure_map(manifest: dict) -> list[dict]:
    rows = []
    fig_exts = (".png", ".jpeg", ".jpg", ".pdf", ".svg", ".tiff", ".tif")
    for rec in manifest.get("executions", []):
        for o in rec.get("outputs", []):
            low = o.get("path", "").lower()
            if any(low.endswith(e) for e in fig_exts) and "sha256" in o:
                rows.append({
                    "figure": o["path"],
                    "figure_sha256": o["sha256"],
                    "script": (rec.get("code") or {}).get("path") or rec.get("command"),
                    "env": rec.get("env"),
                    "seed": rec.get("seed"),
                    "inputs": ", ".join(i.get("path") for i in rec.get("inputs", [])),
                    "exit_code": rec.get("exit_code"),
                })
    return rows


def cmd_report(args: argparse.Namespace) -> int:
    run_dir = _run_dir(args.run)
    manifest = load_manifest(run_dir)
    run_id = manifest.get("run_id", os.path.basename(run_dir))

    L = []
    L.append(f"# Reproducibility Appendix — {run_id}")
    L.append("")
    L.append(f"- **Created**: {manifest.get('created_at')}")
    L.append(f"- **Host/user**: {manifest.get('host')} / {manifest.get('user')}")
    L.append(f"- **Project**: {manifest.get('project')}")
    L.append(f"- **Research question**: {manifest.get('research_question') or '(not recorded)'}")
    L.append(f"- **Seed**: {manifest.get('seed') or '(not recorded)'}")
    L.append("")

    git = manifest.get("git")
    L.append("## Git state")
    if git:
        L.append(f"- Commit: `{git['commit']}`")
        L.append(f"- Branch: `{git['branch']}`")
        L.append(f"- Dirty files: {git['dirty_files']}")
        L.append(f"- Dirty-tree hash: `{git['dirty_tree_hash']}`")
    else:
        L.append("- (not a git repository)")
    L.append("")

    L.append("## Environment")
    env = manifest.get("environment", {})
    for rec in env.get("conda_envs", []):
        if "error" in rec:
            L.append(f"- conda env `{rec['name']}`: {rec['error']}")
        else:
            L.append(f"- conda env `{rec['name']}` → `environment/{rec['file']}` (`{rec['sha256'][:16]}…`)")
    rs = env.get("r_session")
    if rs:
        if "error" in rs:
            L.append(f"- R sessionInfo: {rs['error']}")
        else:
            L.append(f"- R sessionInfo → `environment/{rs['file']}` (`{rs['sha256'][:16]}…`)")
    L.append("")

    L.append("## Executions")
    L.append("| exec | exit | env | seed | command | inputs | outputs |")
    L.append("|------|------|-----|------|---------|--------|---------|")
    for rec in manifest.get("executions", []):
        ins = ", ".join(f"`{i['path']}`" for i in rec.get("inputs", [])) or "—"
        outs = ", ".join(f"`{o['path']}`" for o in rec.get("outputs", [])) or "—"
        L.append(f"| {rec['execution_id']} | {rec['exit_code']} | {rec.get('env') or '—'} | "
                 f"{rec.get('seed') or '—'} | `{rec['command']}` | {ins} | {outs} |")
    L.append("")

    figs = _figure_map(manifest)
    if figs:
        L.append("## Figure → Script → Env → Data → Seed")
        L.append("| figure | script | env | seed | input data |")
        L.append("|--------|--------|-----|------|------------|")
        for r in figs:
            L.append(f"| `{r['figure']}` | `{r['script']}` | {r['env'] or '—'} | "
                     f"{r['seed'] or '—'} | {r['inputs'] or '—'} |")
        L.append("")

    v = manifest.get("verdicts", {})
    L.append("## Verdicts")
    L.append(f"- **Integrity**: {v.get('integrity', '(not yet verified)')}")
    L.append(f"- **Reproduction**: {v.get('reproduction', '(not yet verified)')}")
    L.append("")

    out = args.out or os.path.join(run_dir, "reproducibility_appendix.md")
    with open(out, "w") as f:
        f.write("\n".join(L) + "\n")
    print(out)
    return 0


# --------------------------------------------------------------------------- main

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="provenance", description="Provenance runtime for agentic bioinformatics")
    sub = p.add_subparsers(dest="cmd", required=True)

    pi = sub.add_parser("init", help="start a run")
    pi.add_argument("--project", default=None, help="project dir (default: cwd)")
    pi.add_argument("--question", default="", help="research question / goal")
    pi.add_argument("--seed", default=None, help="global random seed")
    pi.add_argument("--env", action="append", help="conda env name to export (repeatable)")
    pi.add_argument("--rscript", default=None, help="path to Rscript for sessionInfo capture")
    pi.set_defaults(fn=cmd_init)

    pe = sub.add_parser("exec", help="wrap an execution")
    pe.add_argument("--run", required=True, help="run id or path")
    pe.add_argument("--name", default=None, help="human-readable execution name")
    pe.add_argument("--cmd", required=True, help="shell command to run")
    pe.add_argument("--inputs", default="", help="comma-separated input paths")
    pe.add_argument("--outputs", default="", help="comma-separated output paths (hash after run)")
    pe.add_argument("--env", default=None, help="env name used")
    pe.add_argument("--seed", default=None, help="seed used")
    pe.add_argument("--cwd", default=None, help="working dir for the command")
    pe.add_argument("--copy", default=None, help="script path to copy into run's code/ (for unversioned scripts)")
    pe.add_argument("--timeout", type=int, default=600, help="command timeout (s)")
    pe.set_defaults(fn=cmd_exec)

    pv = sub.add_parser("verify", help="verify a run")
    pv.add_argument("--run", required=True, help="run id or path")
    pv.add_argument("--rerun", action="store_true", help="also re-run executions and compare output hashes")
    pv.add_argument("--timeout", type=int, default=600, help="per-execution timeout (s)")
    pv.set_defaults(fn=cmd_verify)

    pr = sub.add_parser("report", help="emit reproducibility appendix")
    pr.add_argument("--run", required=True, help="run id or path")
    pr.add_argument("--out", default=None, help="output markdown path (default: inside run dir)")
    pr.set_defaults(fn=cmd_report)

    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
