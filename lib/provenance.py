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
import re
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
    manifest.setdefault("decisions", []).append(ev)
    save_manifest(run_dir, manifest)
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


# --------------------------------------------------------------------------- check
# provenance-check: a discipline linter over a run's manifest + event log.
# FAIL = procedure violation (mislabeled exec, secret recorded, missing init).
# WARN = honest gap or unaddressed risk (no env capture, reproduction differs).
# Verdicts: PROVENANCE_CHECK_PASS / PROVENANCE_CHECK_FAIL.

SECRET_PATTERNS = [
    r"(?i)\b(api[_-]?key|apikey|password|passwd|client[_-]?secret|secret|access[_-]?token|auth[_-]?token|bearer|authorization)\b\s*[:=]\s*[\"']?[^\s\"']{6,}",
    r"\b(gho_|ghp_|github_pat_|sk-[A-Za-z0-9]{20,}|xox[baprs]-|AKIA[0-9A-Z]{16})\b",
]


def scan_secrets(text: str) -> list[str]:
    """Return matched secret-shaped strings (truncated), deduplicated."""
    hits: set[str] = set()
    for pat in SECRET_PATTERNS:
        for m in re.finditer(pat, text):
            hits.add(m.group(0)[:48])
    return sorted(hits)


def cmd_check(args: argparse.Namespace) -> int:
    run_dir = _run_dir(args.run)
    manifest = load_manifest(run_dir)
    events = load_events(run_dir)
    checks: list[dict] = []

    def add(severity: str, check: str, ok: bool, detail: str = "") -> None:
        checks.append({"level": "OK" if ok else severity,
                       "check": check, "ok": ok, "detail": detail})

    # --- lifecycle ---
    has_start = any(e.get("event") == "run_started" for e in events)
    add("FAIL", "init: run_started event present", has_start,
        "no run_started event — was provenance-init called?" if not has_start else "")

    # --- git ---
    git = manifest.get("git")
    add("WARN", "git state captured", bool(git),
        "not a git repository" if not git else f"commit {str(git.get('commit',''))[:8]}")

    # --- environment ---
    caps = manifest.get("environment", {}).get("captures", [])
    have_cap = any("sha256" in c for c in caps)
    add("WARN", "environment captured (or honest error)", have_cap,
        f"{len(caps)} captures recorded" if caps else "no captures recorded")

    # --- executions ---
    execs = manifest.get("executions", [])
    unlabeled = [e["execution_id"] for e in execs
                 if e.get("evidence_label") not in EVIDENCE_LABELS]
    add("FAIL", "all executions have valid evidence labels", not unlabeled,
        "unlabeled: " + ", ".join(unlabeled) if unlabeled else "")

    nocmd = [e["execution_id"] for e in execs
             if not str(e.get("command") or "").strip()]
    add("FAIL", "all executions have a recorded command", not nocmd,
        "missing command: " + ", ".join(nocmd) if nocmd else "")

    unhashed_out = sorted({e["execution_id"] for e in execs
                           for o in e.get("outputs", []) if "sha256" not in o})
    add("WARN", "all outputs hashed", not unhashed_out,
        "unhashed outputs in: " + ", ".join(unhashed_out) if unhashed_out else "")

    unhashed_in = sorted({e["execution_id"] for e in execs
                          for i in e.get("inputs", []) if "sha256" not in i})
    add("WARN", "all inputs hashed", not unhashed_in,
        "unhashed inputs in: " + ", ".join(unhashed_in) if unhashed_in else "")

    weak = [e["execution_id"] for e in execs
            if e.get("evidence_label") in ("adopted", "inferred")
            and not (e.get("notes") or e.get("decision_id"))]
    add("WARN", "adopted/inferred executions documented", not weak,
        "undocumented weak evidence: " + ", ".join(weak) if weak else "")

    # --- secrets (manifest + events + captured stdout/stderr logs) ---
    blob = json.dumps(manifest, sort_keys=True)
    for e in events:
        blob += "\n" + json.dumps(e, sort_keys=True)
    for rec in execs:
        for logf in ("stdout_log", "stderr_log"):
            fp = os.path.join(run_dir, str(rec.get(logf, "")))
            if os.path.exists(fp):
                with open(fp, encoding="utf-8", errors="replace") as f:
                    blob += f.read(1 << 20)
    hits = scan_secrets(blob)
    add("FAIL", "no secrets recorded", not hits,
        "possible secrets: " + ", ".join(hits) if hits else "")

    # --- verify / report discipline ---
    has_report = os.path.exists(os.path.join(run_dir, "reproducibility_appendix.md"))
    has_verify = any(str(e.get("event", "")).startswith("verify_") for e in events)
    add("WARN", "verify recorded before report", (not has_report) or has_verify,
        "report exists but no verify event recorded" if has_report and not has_verify else "")

    repro = manifest.get("verdicts", {}).get("reproduction")
    if repro == "REPRODUCTION_DIFFERS":
        add("WARN", "reproduction differences addressed", False,
            "re-run differs — mark expected-nondeterministic or investigate")
    else:
        add("WARN", "reproduction differences addressed", True,
            f"verdict: {repro or 'not run'}")

    # --- verdict ---
    fails = [c for c in checks if c["level"] == "FAIL"]
    warns = [c for c in checks if c["level"] == "WARN" and not c["ok"]]
    verdict = "PROVENANCE_CHECK_PASS" if not fails else "PROVENANCE_CHECK_FAIL"

    lines = [f"# Provenance Check — {os.path.basename(run_dir)}", ""]
    lines.append("| severity | check | status | detail |")
    lines.append("|----------|-------|--------|--------|")
    for c in checks:
        mark = "✔" if c["ok"] else ("⚠" if c["level"] == "WARN" else "✖")
        lines.append(f"| {c['level']} | {c['check']} | {mark} | {c['detail']} |")
    lines.append("")
    lines.append(f"**Verdict**: {verdict} ({len(fails)} fail, {len(warns)} warn)")
    lines.append("")

    out = os.path.join(run_dir, "provenance_check.md")
    with open(out, "w") as f:
        f.write("\n".join(lines) + "\n")

    append_event(run_dir, {"event": "check_completed", "verdict": verdict,
                           "fails": len(fails), "warns": len(warns)})
    manifest.setdefault("verdicts", {})["check"] = verdict
    save_manifest(run_dir, manifest)

    print("\n".join(lines))
    return 0 if not fails else 1


# --------------------------------------------------------------------------- graph
# Graph extraction + rendering from a run's manifest.
#
# Nodes: run, execution, artifact, environment, decision.
# Edges:
#   execution --produces--> artifact   (output with sha256)
#   artifact  --consumed-by--> execution (input with sha256)
#   execution --env--> environment      (by env name if recorded)
#   decision  --motivated-by--> execution (by decision_id if referenced in notes)
#   run       --contains--> execution
# Implicit execution-dependency edges: exec B's input hash == exec A's output hash
#   => A --> B (the pipeline DAG). Computed by hash matching.


def graph_extract(manifest: dict) -> dict:
    nodes: list[dict] = []
    edges: list[dict] = []
    seen: set[str] = set()

    def add_node(kind: str, nid: str, label: str, **kw):
        if nid in seen:
            return
        seen.add(nid)
        nodes.append({"kind": kind, "id": nid, "label": label, **kw})

    def add_edge(src: str, dst: str, rel: str, **kw):
        edges.append({"src": src, "dst": dst, "rel": rel, **kw})

    run_id = manifest.get("run_id") or os.path.basename(
        os.path.dirname(manifest.get("manifest_path", ".")))
    add_node("run", "run", run_id,
             seed=manifest.get("seed"), question=manifest.get("research_question"))

    execs = manifest.get("executions", [])
    envs = manifest.get("environment", {}).get("captures", [])
    decs = manifest.get("decisions", [])

    for e in envs:
        nid = "env_" + str(e.get("method") or "") + "_" + str(e.get("name") or e.get("image") or e.get("path") or "")
        nid = re.sub(r"[^A-Za-z0-9_]", "_", nid)
        add_node("environment", nid, e.get("method", "env"),
                 confidence=e.get("confidence"), error=e.get("error"),
                 sha256=e.get("sha256", "")[:12])

    for d in decs:
        nid = d.get("decision_id", "dec")
        add_node("decision", nid, d.get("decision_id"),
                 confidence=d.get("confidence"), choice=d.get("choice") or "")

    art_meta = manifest.get("artifacts", {})

    for x in execs:
        nid = x.get("execution_id", "")
        label = f"{nid}: {x.get('name','')}"
        add_node("execution", nid, label,
                 cmd=x.get("command", ""), exit_code=x.get("exit_code"),
                 label_e=x.get("evidence_label"), env=x.get("env"),
                 seed=x.get("seed"), expected_nondet=x.get("expected_nondeterministic", False))
        add_edge("run", nid, "contains")
        for o in x.get("outputs", []):
            if "sha256" not in o:
                continue
            aid = o["sha256"]
            add_node("artifact", "art_" + aid[:12], o.get("path", aid[:12]),
                     sha256=aid, size=o.get("size_bytes"), role="output")
            add_edge(nid, "art_" + aid[:12], "produces", path=o.get("path"))
        for i in x.get("inputs", []):
            if "sha256" not in i:
                continue
            aid = i["sha256"]
            add_node("artifact", "art_" + aid[:12], i.get("path", aid[:12]),
                     sha256=aid, size=i.get("size_bytes"), role="input")
            add_edge("art_" + aid[:12], nid, "consumed-by", path=i.get("path"))
        # execution -> env edge by recorded env name
        if x.get("env"):
            for e in envs:
                if str(e.get("name") or "") == str(x["env"]):
                    add_edge(nid, "env_" + str(e.get("method")) + "_" + str(e.get("name")), "env", env_name=x["env"])
        # decision -> execution edge when decision referenced (notes contain decision_id)
        notes = str(x.get("notes") or "")
        for d in decs:
            if d.get("decision_id") and d["decision_id"] in notes:
                add_edge(d.get("decision_id"), nid, "motivated")

    # Implicit execution dependencies via output->input hash match
    out_map: dict[str, str] = {}   # sha -> producing exec
    for x in execs:
        for o in x.get("outputs", []):
            if "sha256" in o:
                out_map[o["sha256"]] = x["execution_id"]
    in_map: dict[str, list[str]] = {}  # sha -> consuming execs
    for x in execs:
        for i in x.get("inputs", []):
            if "sha256" in i:
                in_map.setdefault(i["sha256"], []).append(x["execution_id"])
    for h, prod in out_map.items():
        for cons in in_map.get(h, []):
            if prod != cons:
                add_edge(prod, cons, "depends-on")

    return {"run_id": run_id, "nodes": nodes, "edges": edges}


def _esc(s: str) -> str:
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def render_mermaid(g: dict) -> str:
    color = {"run": "#f9f0ff", "execution": "#e6f2ff", "artifact": "#e6ffe6",
             "environment": "#fff7e6", "decision": "#ffe6e6"}
    border = {"run": "#9b59b6", "execution": "#2e86c1", "artifact": "#27ae60",
              "environment": "#e67e22", "decision": "#c0392b"}
    L = ["```mermaid", "flowchart LR"]
    for n in g["nodes"]:
        lbl = n["label"]
        if n["kind"] == "execution":
            lbl = f"{n['label']}<br/>exit={n.get('exit_code')} [{n.get('label_e')}]"
        if n["kind"] == "artifact":
            lbl = f"{n['label']}<br/>{n.get('sha256','')[:12]}"
        if n["kind"] == "environment" and n.get("error"):
            lbl = f"{n['label']}<br/>ERROR: {n['error']}"
        L.append(f'    {n["id"]}["{lbl}"]:::{n["kind"]}')
    for e in g["edges"]:
        L.append(f'    {e["src"]} -->|{e["rel"]}| {e["dst"]}')
    L.append("    classDef run fill:#f9f0ff,stroke:#9b59b6;")
    L.append("    classDef execution fill:#e6f2ff,stroke:#2e86c1;")
    L.append("    classDef artifact fill:#e6ffe6,stroke:#27ae60;")
    L.append("    classDef environment fill:#fff7e6,stroke:#e67e22;")
    L.append("    classDef decision fill:#ffe6e6,stroke:#c0392b;")
    L.append("```")
    return "\n".join(L)


def render_dot(g: dict) -> str:
    color = {"run": "#f9f0ff", "execution": "#e6f2ff", "artifact": "#e6ffe6",
             "environment": "#fff7e6", "decision": "#ffe6e6"}
    L = ['digraph G {', '  rankdir=LR;', '  node [shape=box, style="rounded,filled", fontname="Helvetica"];', '  edge [color="#666666"];']
    for n in g["nodes"]:
        L.append(f'  {n["id"]} [label="{_esc(n["label"])}", fillcolor="{color[n["kind"]]}"];')
    for e in g["edges"]:
        L.append(f'  {e["src"]} -> {e["dst"]} [label="{e["rel"]}"];')
    L.append("}")
    return "\n".join(L)


def render_ascii(g: dict) -> str:
    # Layered view: run -> executions with their inputs/outputs indented.
    L = [f"{g['run_id']} (run)"]
    for n in g["nodes"]:
        if n["kind"] != "execution":
            continue
        L.append(f"  {n['label']}  [{n.get('label_e')}] exit={n.get('exit_code')} env={n.get('env') or '-'} seed={n.get('seed') or '-'}")
        for e in g["edges"]:
            if e["src"] == n["id"] and e["rel"] == "produces":
                art = next((a for a in g["nodes"] if a["id"] == e["dst"]), None)
                L.append(f"    -> {e['rel']}: {art['label'] if art else e['dst']}  [{art.get('sha256','')[:12] if art else ''}]") if art else None
        for e in g["edges"]:
            if e["dst"] == n["id"] and e["rel"] == "consumed-by":
                art = next((a for a in g["nodes"] if a["id"] == e["src"]), None)
                L.append(f"    <- {e['rel']}: {art['label'] if art else e['src']}  [{art.get('sha256','')[:12] if art else ''}]") if art else None
    # dependency edges
    deps = [e for e in g["edges"] if e["rel"] == "depends-on"]
    if deps:
        L.append("  pipeline: " + " -> ".join(f"{e['src']}->{e['dst']}" for e in deps))
    return "\n".join(L)


def render_json(g: dict) -> str:
    return json.dumps(g, indent=2)


def _layered_layout(g: dict) -> tuple[dict, dict]:
    """Minimal longest-path layering (stdlib-only). Returns {id:(x,y)} and {id:(w,h)}."""
    preds: dict[str, list[str]] = {n["id"]: [] for n in g["nodes"]}
    for e in g["edges"]:
        preds.setdefault(e["dst"], []).append(e["src"])
    layer: dict[str, int] = {}
    for n in g["nodes"]:
        layer[n["id"]] = 0
    # longest path (simple fixed-point; graphs here are small DAGs)
    changed = True
    while changed:
        changed = False
        for n in g["nodes"]:
            for p in preds.get(n["id"], []):
                if layer[p] + 1 > layer[n["id"]]:
                    layer[n["id"]] = layer[p] + 1
                    changed = True
    layers: dict[int, list[str]] = {}
    for nid, l in layer.items():
        layers.setdefault(l, []).append(nid)
    pos: dict[str, tuple[float, float]] = {}
    size: dict[str, tuple[float, float]] = {}
    W, H = 220.0, 70.0
    for l, ids in sorted(layers.items()):
        for i, nid in enumerate(ids):
            pos[nid] = (l * (W + 60) + 20, i * (H + 40) + 20)
            size[nid] = (W, H)
    return pos, size


def render_html(g: dict) -> str:
    pos, size = _layered_layout(g)
    W = max((p[0] + s[0] for p, s in zip(pos.values(), size.values())), default=200) + 40
    H = max((p[1] + s[1] for p, s in zip(pos.values(), size.values())), default=200) + 40
    fill = {"run": "#f9f0ff", "execution": "#e6f2ff", "artifact": "#e6ffe6",
            "environment": "#fff7e6", "decision": "#ffe6e6"}
    stroke = {"run": "#9b59b6", "execution": "#2e86c1", "artifact": "#27ae60",
              "environment": "#e67e22", "decision": "#c0392b"}
    parts = []
    for e in g["edges"]:
        if e["src"] not in pos or e["dst"] not in pos:
            continue
        (x1, y1), (x2, y2) = pos[e["src"]], pos[e["dst"]]
        s1, s2 = size[e["src"]], size[e["dst"]]
        # edge from right of src to left of dst
        mx, my = (x1 + s1[0] + x2) / 2, (y1 + s1[1] / 2 + y2 + s2[1] / 2) / 2
        d = f'M{x1 + s1[0]},{y1 + s1[1] / 2} C{mx},{y1 + s1[1] / 2} {mx},{y2 + s2[1] / 2} {x2},{y2 + s2[1] / 2}'
        parts.append(f'<path d="{d}" fill="none" stroke="#999" stroke-width="1.2"/>')
        parts.append(f'<text x="{mx}" y="{(y1 + s1[1] / 2 + y2 + s2[1] / 2) / 2 - 6}" font-size="10" fill="#666" text-anchor="middle">{_esc(e["rel"])}</text>')
    for n in g["nodes"]:
        x, y = pos[n["id"]]
        w, h = size[n["id"]]
        title = n["label"]
        if n["kind"] == "artifact":
            title = f"{n['label']}\nsha256: {n.get('sha256','')}"
        if n["kind"] == "execution":
            title = f"{n['label']}\nexit={n.get('exit_code')} [{n.get('label_e')}]\n{n.get('cmd','')}"
        parts.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="8" fill="{fill[n["kind"]]}" stroke="{stroke[n["kind"]]}" stroke-width="1.5"><title>{_esc(title)}</title></rect>')
        parts.append(f'<text x="{x + 8}" y="{y + 18}" font-size="11" font-family="sans-serif">{_esc(n["label"])}</text>')
        if n["kind"] == "execution":
            parts.append(f'<text x="{x + 8}" y="{y + 36}" font-size="9" font-family="monospace" fill="#444">exit={n.get("exit_code")} [{n.get("label_e")}]</text>')
            parts.append(f'<text x="{x + 8}" y="{y + 50}" font-size="9" font-family="monospace" fill="#666">{_esc(n.get("cmd",""))[:36]}</text>')
        if n["kind"] == "artifact":
            parts.append(f'<text x="{x + 8}" y="{y + 36}" font-size="9" font-family="monospace" fill="#666">{n.get("sha256","")[:12]}</text>')
        if n["kind"] == "environment":
            parts.append(f'<text x="{x + 8}" y="{y + 36}" font-size="9" font-family="monospace" fill="#666">conf={n.get("confidence") or "-"}{(" ERROR" if n.get("error") else "")}</text>')
        if n["kind"] == "decision":
            parts.append(f'<text x="{x + 8}" y="{y + 36}" font-size="9" font-family="monospace" fill="#666">conf={n.get("confidence") or "-"}</text>')
    svg = f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}">' + "".join(parts) + "</svg>"
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Reproducibility graph — {_esc(g['run_id'])}</title>
<style>body{{font-family:sans-serif;margin:24px}} h1{{font-size:18px}} .legend span{{display:inline-block;padding:2px 10px;margin-right:8px;border-radius:6px;font-size:12px}}</style>
</head><body>
<h1>Reproducibility graph — {_esc(g['run_id'])}</h1>
<div class="legend">
  <span style="background:#f9f0ff;border:1px solid #9b59b6">run</span>
  <span style="background:#e6f2ff;border:1px solid #2e86c1">execution</span>
  <span style="background:#e6ffe6;border:1px solid #27ae60">artifact</span>
  <span style="background:#fff7e6;border:1px solid #e67e22">environment</span>
  <span style="background:#ffe6e6;border:1px solid #c0392b">decision</span>
</div>
{svg}
<p style="font-size:11px;color:#666">Hover a node for sha256 / exit code / command. Edge labels: produces / consumed-by / env / depends-on.</p>
</body></html>"""


def cmd_graph(args: argparse.Namespace) -> int:
    run_dir = _run_dir(args.run)
    manifest = load_manifest(run_dir)
    g = graph_extract(manifest)
    out = None
    if args.format == "mermaid":
        text = render_mermaid(g)
        out = os.path.join(run_dir, "graph.mmd")
    elif args.format == "dot":
        text = render_dot(g)
        out = os.path.join(run_dir, "graph.dot")
    elif args.format == "html":
        text = render_html(g)
        out = os.path.join(run_dir, "graph.html")
    elif args.format == "ascii":
        text = render_ascii(g)
        out = os.path.join(run_dir, "graph.txt")
    else:
        text = render_json(g)
        out = os.path.join(run_dir, "graph.json")
    with open(out, "w") as f:
        f.write(text + ("\n" if args.format != "html" else ""))
    append_event(run_dir, {"event": "graph_rendered", "format": args.format, "out": out})
    print(out)
    return 0


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

    pck = sub.add_parser("check", help="audit a run for provenance discipline (linter)")
    pck.add_argument("--run", required=True, help="run id or path")
    pck.set_defaults(fn=cmd_check)

    pg = sub.add_parser("graph", help="render the reproducibility graph")
    pg.add_argument("--run", required=True, help="run id or path")
    pg.add_argument("--format", choices=["mermaid", "dot", "html", "ascii", "json"],
                    default="mermaid", help="output format")
    pg.set_defaults(fn=cmd_graph)

    pr = sub.add_parser("report", help="emit reproducibility appendix")
    pr.add_argument("--run", required=True, help="run id or path")
    pr.add_argument("--out", default=None, help="output markdown path (default: inside run dir)")
    pr.set_defaults(fn=cmd_report)

    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
