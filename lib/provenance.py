#!/usr/bin/env python3
"""provenance.py — deterministic execution layer for agentic bioinformatics provenance.

Self-contained (Python 3 stdlib only, zero dependencies). Works locally or over SSH.

ARCHITECTURE — LLM decides, runtime executes:
  The LLM (pi agent + skills) is the DECISION layer: it detects the environment, decides
  what to capture, and authors CAPTURE PLANS + DECISION events. This runtime is the
  EXECUTION layer: it executes plans deterministically (hash / capture / record / verify /
  report). The LLM never computes hashes or writes JSON itself; it authors the plan and
  the runtime enforces it. Decisions are recorded as first-class provenance events so the
  edge-case handling is itself auditable.

Evidence labels (per execution):
  OBSERVED   — agent wrapped the execution (strongest)
  ADOPTED    — recorded after the fact; exit_code may be unknown (medium)
  INFERRED   — lineage asserted by a human, not observed (weakest)

Verdicts:
  PROVENANCE_COMPLETE / PROVENANCE_INCOMPLETE   integrity of recorded artifacts
  REPRODUCTION_VERIFIED / REPRODUCTION_DIFFERS  re-run matched / differed
  VERIFIED_WITH_CAVEAT                          expected-nondeterministic exec matched

Design rules:
  - Path = location, SHA-256 = identity. Never trust paths alone.
  - The provenance graph is the index; large artifacts stay in place on disk.
  - The event log (provenance.jsonl) is append-only source of truth; manifest.json is a
    materialized summary.
  - Failed runs are recorded, not discarded.
  - Secrets are never recorded.
  - Full SHA-256 always (streaming, 1 MiB chunks) — no fast mode for large files.
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

SCHEMA = "baird-provenance/v2"
CHUNK = 1 << 20  # 1 MiB
RESULTS_DIR = "results"
RUN_DIR_FMT = "run_%Y%m%d_%H%M%S"

# Confidence levels for capture methods.
CONFIDENCE = ("high", "medium", "low")
EVIDENCE_LABELS = ("observed", "adopted", "inferred")


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


# --------------------------------------------------------------------------- capture methods
# Each method: identifier -> (name, confidence, capture_fn(out_dir, kwargs) -> record dict)
# The registry is OPEN: the LLM composes these primitives for new edge cases and records a
# DECISION event explaining the choice. Confidence labels the evidence strength.

_CAPTURE_METHODS: dict[str, dict] = {}


def register_capture(method: str, name: str, confidence: str, fn) -> None:
    _CAPTURE_METHODS[method] = {"name": name, "confidence": confidence, "fn": fn}


def _cap_git(out_dir: str, kwargs: dict) -> dict:
    cwd = kwargs.get("project") or kwargs.get("cwd") or os.getcwd()
    g = git_state(cwd)
    if not g:
        return {"method": "git", "error": "not a git repository"}
    return {"method": "git", **g}


def _cap_conda(out_dir: str, kwargs: dict) -> dict:
    env_name = kwargs.get("env") or kwargs.get("name")
    if not env_name:
        return {"method": "conda", "error": "missing env name"}
    out = os.path.join(out_dir, f"env_{env_name}.yml")
    for mgr in ("conda", "mamba", "micromamba"):
        rc, stdout, _ = run_cmd(f"{mgr} env export -n {env_name} 2>/dev/null", timeout=300)
        if rc == 0 and stdout.strip():
            with open(out, "w") as f:
                f.write(stdout)
            return {"method": "conda", "name": env_name, "manager": mgr,
                    "file": os.path.basename(out), "sha256": sha256_file(out)}
    return {"method": "conda", "name": env_name, "error": "conda/mamba/micromamba env export failed"}


def _cap_pixi(out_dir: str, kwargs: dict) -> dict:
    cwd = kwargs.get("cwd") or os.getcwd()
    # Best pin: copy the lockfile verbatim.
    for lock in ("pixi.lock", "pixi.toml"):
        p = os.path.join(cwd, lock)
        if os.path.exists(p):
            dest = os.path.join(out_dir, lock)
            with open(p, "rb") as fi, open(dest, "wb") as fo:
                fo.write(fi.read())
            return {"method": "pixi", "file": lock, "sha256": sha256_file(dest)}
    rc, stdout, _ = run_cmd("pixi export 2>/dev/null", timeout=300, cwd=cwd)
    if rc == 0 and stdout.strip():
        out = os.path.join(out_dir, "pixi_export.yml")
        with open(out, "w") as f:
            f.write(stdout)
        return {"method": "pixi", "file": "pixi_export.yml", "sha256": sha256_file(out)}
    return {"method": "pixi", "error": "no pixi.lock/pixi.toml and pixi export failed"}


def _cap_docker(out_dir: str, kwargs: dict) -> dict:
    image = kwargs.get("image")
    rc, stdout, _ = run_cmd(f"docker image inspect {image} 2>/dev/null", timeout=120)
    if rc != 0 or not stdout.strip():
        return {"method": "docker", "image": image, "error": "docker image inspect failed"}
    rec = json.loads(stdout)[0]
    digest = rec.get("Id", "").replace("sha256:", "")
    out = os.path.join(out_dir, f"docker_{image.replace('/', '_').replace(':', '_')}.json")
    with open(out, "w") as f:
        json.dump({"Id": rec.get("Id"), "RepoTags": rec.get("RepoTags"),
                   "Architecture": rec.get("Architecture"), "Os": rec.get("Os"),
                   "Created": rec.get("Created")}, f, indent=2)
    return {"method": "docker", "image": image, "digest": digest,
            "file": os.path.basename(out), "sha256": sha256_file(out)}


def _cap_renv(out_dir: str, kwargs: dict) -> dict:
    cwd = kwargs.get("cwd") or os.getcwd()
    lock = os.path.join(cwd, "renv.lock")
    if not os.path.exists(lock):
        return {"method": "renv", "error": "no renv.lock"}
    dest = os.path.join(out_dir, "renv.lock")
    with open(lock, "rb") as fi, open(dest, "wb") as fo:
        fo.write(fi.read())
    return {"method": "renv", "file": "renv.lock", "sha256": sha256_file(dest)}


def _cap_pip(out_dir: str, kwargs: dict) -> dict:
    name = kwargs.get("name") or "pip"
    out = os.path.join(out_dir, f"pip_freeze_{name}.txt")
    rc, stdout, _ = run_cmd("pip freeze 2>/dev/null", timeout=300,
                            cwd=kwargs.get("cwd"))
    if rc != 0 or not stdout.strip():
        return {"method": "pip", "error": "pip freeze failed"}
    with open(out, "w") as f:
        f.write(stdout)
    return {"method": "pip", "name": name, "file": os.path.basename(out),
            "sha256": sha256_file(out)}


def _cap_rsession(out_dir: str, kwargs: dict) -> dict:
    rscript = kwargs.get("rscript") or "Rscript"
    name = kwargs.get("name") or "R"
    out = os.path.join(out_dir, f"sessionInfo_{name.replace(' ', '_')}.txt")
    rc, stdout, _ = run_cmd(f"{rscript} -e 'sessionInfo()' 2>/dev/null", timeout=300)
    if rc != 0 or not stdout.strip():
        return {"method": "rsession", "error": "R sessionInfo capture failed"}
    with open(out, "w") as f:
        f.write(stdout)
    return {"method": "rsession", "name": name, "file": os.path.basename(out),
            "sha256": sha256_file(out)}


def _cap_sacct(out_dir: str, kwargs: dict) -> dict:
    jobid = kwargs.get("job_id")
    if not jobid:
        return {"method": "sacct", "error": "missing job_id"}
    rc, stdout, _ = run_cmd(f"sacct -j {jobid} --format=JobID,State,ExitCode "
                            "--noheader 2>/dev/null", timeout=60)
    if rc != 0 or not stdout.strip():
        return {"method": "sacct", "job_id": jobid, "error": "sacct query failed"}
    with open(os.path.join(out_dir, f"sacct_{jobid}.txt"), "w") as f:
        f.write(stdout)
    return {"method": "sacct", "job_id": jobid, "state": stdout.strip().splitlines()[-1],
            "file": f"sacct_{jobid}.txt", "sha256": sha256_file(os.path.join(out_dir, f"sacct_{jobid}.txt"))}


def _cap_file(out_dir: str, kwargs: dict) -> dict:
    """Hash/copy-in a single file or directory (e.g. a lockfile, a downloaded artifact)."""
    path = kwargs.get("path")
    if not path or not os.path.exists(path):
        return {"method": "file", "path": path, "error": "missing at capture time"}
    ap = os.path.abspath(path)
    rec = {"method": "file", "path": path, "sha256": sha256_path(ap)}
    if os.path.isfile(ap):
        rec["size_bytes"] = os.path.getsize(ap)
        # Copy small files into the run (for lockfile/script captures); leave big ones in place.
        if kwargs.get("copy") and os.path.getsize(ap) < 10 * CHUNK:
            dest = os.path.join(out_dir, os.path.basename(ap))
            with open(ap, "rb") as fi, open(dest, "wb") as fo:
                while True:
                    blk = fi.read(CHUNK)
                    if not blk:
                        break
                    fo.write(blk)
            rec["file"] = os.path.basename(dest)
    return rec


def register_default_captures() -> None:
    register_capture("git", "git state", "high", _cap_git)
    register_capture("conda", "conda/mamba/micromamba env export", "high", _cap_conda)
    register_capture("pixi", "pixi lockfile / export", "high", _cap_pixi)
    register_capture("docker", "docker image inspect", "high", _cap_docker)
    register_capture("renv", "renv.lock", "high", _cap_renv)
    register_capture("pip", "pip freeze", "medium", _cap_pip)
    register_capture("rsession", "R sessionInfo", "high", _cap_rsession)
    register_capture("sacct", "slurm sacct", "high", _cap_sacct)
    register_capture("file", "file/dir hash + optional copy", "high", _cap_file)


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
        "environment": {"captures": []},
        "executions": [],
        "artifacts": {},
        "verdicts": {},
    }

    save_manifest(run_dir, manifest)
    append_event(run_dir, {"event": "run_started", "run_id": manifest["run_id"], "seed": args.seed})

    print(run_dir)
    return 0


# --------------------------------------------------------------------------- capture command

def cmd_capture(args: argparse.Namespace) -> int:
    run_dir = _run_dir(args.run)
    manifest = load_manifest(run_dir)
    out_dir = os.path.join(run_dir, "environment")
    os.makedirs(out_dir, exist_ok=True)

    method = args.method
    info = _CAPTURE_METHODS.get(method)
    if not info:
        sys.exit(f"ERROR: unknown capture method '{method}'. Known: {', '.join(sorted(_CAPTURE_METHODS))}")

    kwargs = {}
    for spec in (args.kwargs or []):
        k, _, v = spec.partition("=")
        if k.strip():
            kwargs[k.strip()] = v

    rec = info["fn"](out_dir, kwargs)
    rec.setdefault("confidence", info["confidence"])
    rec.setdefault("recorded_at", now_iso())

    manifest.setdefault("environment", {}).setdefault("captures", []).append(rec)
    save_manifest(run_dir, manifest)
    append_event(run_dir, {"event": "capture", "method": method, "rec": rec})
    print(json.dumps(rec, indent=2))
    return 0 if "error" not in rec else 1


def cmd_record_decision(args: argparse.Namespace) -> int:
    run_dir = _run_dir(args.run)
    manifest = load_manifest(run_dir)
    ev = {
        "event": "decision",
        "decision_id": args.id or f"dec_{len(load_events(run_dir)) + 1:03d}",
        "situation": args.situation,
        "choice": args.choice,
        "reason": args.reason,
        "confidence": args.confidence,
    }
    if args.evidence:
        ev["evidence"] = args.evidence.split(",")
    if args.hashes:
        ev["hashes"] = args.hashes.split(",")
    append_event(run_dir, ev)
    print(json.dumps(ev, indent=2))
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

    # For adopted/inferred execs, the caller may know the exit code without re-running
    # (e.g. from a slurm log). Override only when explicitly given.
    if args.exit_code_override is not None:
        rc = int(args.exit_code_override)
        stdout = stdout or "(exit code overridden; stdout not captured)"
        stderr = stderr or "(exit code overridden)"

    # Hash outputs AFTER execution so we record the produced files.
    outputs = _hash_path_list(args.outputs)

    log_base = os.path.join(run_dir, "logs", exec_id)
    with open(log_base + ".stdout.log", "w") as f:
        f.write(stdout or "")
    with open(log_base + ".stderr.log", "w") as f:
        f.write(stderr or "")

    label = (args.evidence_label or "observed").lower()
    if label not in EVIDENCE_LABELS:
        sys.exit(f"ERROR: evidence label must be one of {EVIDENCE_LABELS}, got '{label}'")

    rec = {
        "execution_id": exec_id,
        "name": args.name or exec_id,
        "command": args.cmd,
        "cwd": os.path.abspath(args.cwd) if args.cwd else os.getcwd(),
        "env": args.env,
        "seed": args.seed,
        "evidence_label": label,
        "started_at": now_iso(),
        "duration_s": duration,
        "exit_code": rc,
        "expected_nondeterministic": bool(args.expected_nondeterministic),
        "code": code_ref,
        "inputs": inputs,
        "outputs": outputs,
        "stdout_log": f"logs/{exec_id}.stdout.log",
        "stderr_log": f"logs/{exec_id}.stderr.log",
    }
    if args.notes:
        rec["notes"] = args.notes

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
    env_records = manifest.get("environment", {}).get("captures", [])
    env_ok = any("sha256" in r for r in env_records)
    git_ok = bool(manifest.get("git"))
    verdict = "PROVENANCE_COMPLETE" if (ok and env_ok and git_ok) else "PROVENANCE_INCOMPLETE"
    return verdict, checks


def verify_reproduction(manifest: dict, timeout: int) -> tuple[str, list[dict]]:
    details = []
    all_match = True
    any_caveat = False
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
        # Adopted/inferred runs may have unknown exit codes — treat recorded-unknown as a
        # match if outputs match (re-run success is the strongest signal we have).
        if rec.get("evidence_label") in ("adopted", "inferred") and not rec.get("exit_code_known", True):
            match = out_match
        if rec.get("expected_nondeterministic"):
            match = True
            any_caveat = True
        if not match:
            all_match = False
        details.append({"execution_id": rec["execution_id"], "rerun_exit": rc,
                        "recorded_exit": rec.get("exit_code"), "outputs_match": out_match,
                        "evidence_label": rec.get("evidence_label", "observed"),
                        "expected_nondeterministic": rec.get("expected_nondeterministic", False),
                        "match": match})
    if not all_match:
        return "REPRODUCTION_DIFFERS", details
    if any_caveat:
        return "VERIFIED_WITH_CAVEAT", details
    return "REPRODUCTION_VERIFIED", details


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
                  f"recorded_exit={d['recorded_exit']} outputs_match={d['outputs_match']} "
                  f"label={d.get('evidence_label', 'observed')}")

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
                    "evidence_label": rec.get("evidence_label", "observed"),
                    "inputs": ", ".join(i.get("path") for i in rec.get("inputs", [])),
                    "exit_code": rec.get("exit_code"),
                })
    return rows


def cmd_report(args: argparse.Namespace) -> int:
    run_dir = _run_dir(args.run)
    manifest = load_manifest(run_dir)
    events = load_events(run_dir)
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
    captures = manifest.get("environment", {}).get("captures", [])
    if captures:
        L.append("| method | confidence | detail | sha256 |")
        L.append("|--------|------------|--------|--------|")
        for rec in captures:
            if "error" in rec:
                L.append(f"| {rec.get('method','?')} | {rec.get('confidence','?')} | "
                         f"ERROR: {rec['error']} | — |")
            else:
                detail = rec.get("file") or rec.get("name") or rec.get("image") or rec.get("job_id") or ""
                L.append(f"| {rec.get('method','?')} | {rec.get('confidence','?')} | {detail} | "
                         f"`{rec.get('sha256','')[:16]}…` |")
    else:
        L.append("- (no captures recorded)")
    L.append("")

    L.append("## Executions")
    L.append("| exec | label | exit | env | seed | command | inputs | outputs |")
    L.append("|------|-------|------|-----|------|---------|--------|---------|")
    for rec in manifest.get("executions", []):
        ins = ", ".join(f"`{i['path']}`" for i in rec.get("inputs", [])) or "—"
        outs = ", ".join(f"`{o['path']}`" for o in rec.get("outputs", [])) or "—"
        L.append(f"| {rec['execution_id']} | {rec.get('evidence_label','observed')} | "
                 f"{rec.get('exit_code','?')} | {rec.get('env') or '—'} | "
                 f"{rec.get('seed') or '—'} | `{rec['command']}` | {ins} | {outs} |")
    L.append("")

    figs = _figure_map(manifest)
    if figs:
        L.append("## Figure → Script → Env → Data → Seed")
        L.append("| figure | script | env | seed | evidence | input data |")
        L.append("|--------|--------|-----|------|----------|------------|")
        for r in figs:
            L.append(f"| `{r['figure']}` | `{r['script']}` | {r['env'] or '—'} | "
                     f"{r['seed'] or '—'} | {r['evidence_label']} | {r['inputs'] or '—'} |")
        L.append("")

    decisions = [e for e in events if e.get("event") == "decision"]
    if decisions:
        L.append("## Decisions (edge-case handling)")
        L.append("| decision | situation | choice | confidence | reason |")
        L.append("|----------|-----------|--------|------------|--------|")
        for d in decisions:
            L.append(f"| {d.get('decision_id','?')} | {d.get('situation','?')} | "
                     f"{d.get('choice','?')} | {d.get('confidence','?')} | {d.get('reason','?')} |")
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
    register_default_captures()
    p = argparse.ArgumentParser(prog="provenance", description="Provenance execution layer for agentic bioinformatics")
    sub = p.add_subparsers(dest="cmd", required=True)

    pi = sub.add_parser("init", help="start a run")
    pi.add_argument("--project", default=None, help="project dir (default: cwd)")
    pi.add_argument("--question", default="", help="research question / goal")
    pi.add_argument("--seed", default=None, help="global random seed")
    pi.set_defaults(fn=cmd_init)

    pc = sub.add_parser("capture", help="capture environment state via an open method registry")
    pc.add_argument("--run", required=True, help="run id or path")
    pc.add_argument("--method", required=True, choices=sorted(_CAPTURE_METHODS),
                    help=f"known: {', '.join(sorted(_CAPTURE_METHODS))}")
    pc.add_argument("--kwargs", action="append", default=[],
                    help="key=value capture kwargs (repeatable), e.g. env=R_process7")
    pc.set_defaults(fn=cmd_capture)

    pd = sub.add_parser("record-decision", help="record an LLM-authored decision (edge-case handling)")
    pd.add_argument("--run", required=True)
    pd.add_argument("--id", default=None, help="decision id (default: auto)")
    pd.add_argument("--situation", required=True, help="what triggered the decision")
    pd.add_argument("--choice", required=True, help="what was decided")
    pd.add_argument("--reason", required=True, help="why")
    pd.add_argument("--confidence", default="medium", choices=CONFIDENCE)
    pd.add_argument("--evidence", default=None, help="comma-separated evidence refs")
    pd.add_argument("--hashes", default=None, help="comma-separated evidence hashes")
    pd.set_defaults(fn=cmd_record_decision)

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
    pe.add_argument("--evidence-label", default="observed", choices=EVIDENCE_LABELS,
                    help="observed (agent-wrapped) / adopted (post-hoc) / inferred (asserted)")
    pe.add_argument("--exit-code-override", type=int, default=None,
                    help="known exit code for adopted runs (overrides actual run result)")
    pe.add_argument("--expected-nondeterministic", action="store_true",
                    help="mark exec as expected-nondeterministic (verify -> VERIFIED_WITH_CAVEAT)")
    pe.add_argument("--notes", default=None, help="free-text notes")
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
