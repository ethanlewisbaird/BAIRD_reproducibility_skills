---
name: provenance-init
description: Start a provenance-tracked research run. Creates a dated results/run_<ts>_<id>/ directory with a manifest.json skeleton, an append-only provenance.jsonl event log, and git state. Use at the START of any analysis, before running scripts or generating figures, so that every downstream execution can be traced back. Works locally or over SSH to a compute host (e.g. hibu). Environment capture is done separately via provenance-capture (LLM decides which method).
---

# provenance-init

Starts a provenance-tracked run. This is the "open a run" step of the provenance workflow (see `provenance-capture`, `provenance-exec`, `provenance-adopt`, `provenance-verify`, `provenance-report`).

## When to use

At the start of any scientifically consequential analysis: before running scripts, before generating figures, before any step whose inputs/outputs you might later need to trace. One run per experiment/analysis session.

## Architecture

The **LLM (you) is the decision layer**; the runtime (`../lib/provenance.py`) is the deterministic execution layer. You detect the environment and decide what to capture; the runtime executes deterministically. Your decisions are recorded as `decision` events so the edge-case handling is itself auditable.

## Runtime

`../lib/provenance.py` (stdlib-only Python 3, no dependencies). Run it on the host where results live.

- **Local work:** run directly.
- **Remote host (e.g. hibu):** copy once, then call via SSH:
```bash
scp ~/.pi/agent/skills/provenance/lib/provenance.py hibu:/tmp/provenance.py
ssh hibu "python3 /tmp/provenance.py init --project <dir> ..."
```

## Usage

```bash
python3 provenance.py init \
  --project /path/to/project \   # default: cwd
  --question "research question / goal" \
  --seed 42                      # global random seed
```

Creates `results/run_<YYYYMMDD_HHMMSS>_<id>/`:

```
run_<ts>_<id>/
├── manifest.json         # materialized summary (updated on each event)
├── provenance.jsonl     # append-only event log (source of truth)
├── environment/          # capture records (via provenance-capture)
├── executions/          # per-execution JSON records
├── logs/                # stdout/stderr per execution
├── code/                # optional script copies (via --copy in exec)
└── outputs/             # optional output copies
```

## What init captures

- **Git state** (always): commit hash, branch, dirty-tree hash. If not a git repo, records `null` — the report will note it.
- **Run metadata**: host, user, project, research question, seed.

**Environment capture is NOT done at init anymore.** It's a separate `provenance-capture` step where the LLM detects which package manager / R / Docker is present and picks the right method. Do that next:

```bash
# LLM decides: conda? pixi? docker? renv? pip? R?
python3 provenance.py capture --run <run> --method conda --kwargs env=R_process7
python3 provenance.py capture --run <run> --method rsession --kwargs rscript=/path/to/Rscript
python3 provenance.py capture --run <run> --method git
```

## Rules

- **Never** put secrets, API keys, or credentials into `--question` or any field — they are persisted in plaintext.
- The run id printed by init is the value to pass to `--run` in all other commands.
- For slurm jobs: record the `sbatch` submission as the command; verify outputs later with `provenance-verify --rerun` after the job completes (the exit code of `sbatch` is not the job's exit code). Use `provenance-capture --method sacct --kwargs job_id=<id>` to record the real job state.
