---
name: provenance-init
description: Start a provenance-tracked research run. Creates a dated results/run_<ts>_<id>/ directory with a manifest.json skeleton, an append-only provenance.jsonl event log, git commit + dirty-tree hash, conda env exports, and R sessionInfo captures. Use at the START of any analysis, before running scripts or generating figures, so that every downstream execution can be traced back. Works locally or over SSH to a compute host (e.g. hibu).
---

# provenance-init

Starts a provenance-tracked run. This skill is the "open a run" step of the provenance workflow (see `provenance-exec`, `provenance-verify`, `provenance-report`).

## When to use

At the start of any scientifically consequential analysis: before running scripts, before generating figures, before any step whose inputs/outputs you might later need to trace. One run per experiment/analysis session.

## Runtime

The deterministic runtime is `../lib/provenance.py` (stdlib-only Python 3, no dependencies). It must be run on the host where the results live.

- **Local work:** run it directly.
- **Remote host (e.g. hibu):** copy it there once, then call via SSH:
```bash
scp ~/.pi/agent/skills/provenance/lib/provenance.py hibu:/tmp/provenance.py
ssh hibu "python3 /tmp/provenance.py init --project <dir> ..."
```

## Usage

```bash
python3 provenance.py init \
  --project /path/to/project \   # default: cwd
  --question "research question / goal" \
  --seed 42 \                    # global random seed
  --env R_process7 \             # conda env to export (repeatable)
  --rscript /path/to/Rscript     # capture R sessionInfo (repeatable if multiple)
```

Creates `results/run_<YYYYMMDD_HHMMSS>_<id>/` with:

```
run_<ts>_<id>/
├── manifest.json         # materialized summary (updated on each event)
├── provenance.jsonl     # append-only event log (source of truth)
├── environment/          # git_state.txt, env_<name>.yml, sessionInfo.txt
├── executions/          # per-execution JSON records
├── logs/                # stdout/stderr per execution
├── code/                # optional script copies (via --copy in exec)
└── outputs/             # optional output copies
```

## What it captures (best-effort, never fatal)

- **Git**: commit hash, branch, dirty-tree hash (SHA-256 of `git status --porcelain`). If not a git repo, records `null` — the report will note it.
- **Conda envs**: full `conda env export` (or `micromamba`) for each `--env`, saved to `environment/env_<name>.yml` with its SHA-256.
- **R**: `sessionInfo()` for each `--rscript`, saved to `environment/sessionInfo.txt`.

If a capture fails (e.g. conda not installed), it records the error instead of crashing — the verdict in `provenance-verify` will reflect the gap honestly.

## Rules

- **Never** put secrets, API keys, or credentials into `--question` or any field — they are persisted in plaintext.
- The run id printed by init is the value to pass to `--run` in `provenance-exec` / `provenance-verify` / `provenance-report`.
- For slurm jobs: record the `sbatch` submission as the command; verify outputs later with `provenance-verify --rerun` after the job completes (the exit code of `sbatch` is not the job's exit code).
