---
name: provenance-exec
description: Wrap a single computation with provenance capture — hash inputs, run the command capturing stdout/stderr, hash outputs, record exit code, env, seed, and git state. Use for every scientifically consequential execution (analysis scripts, figure generation, data processing) inside a run started by provenance-init. Works locally or over SSH to a compute host.
---

# provenance-exec

Records one execution inside a run. This is the "record what happened" step of the provenance workflow.

## When to use

For every scientifically consequential command: analysis scripts, figure generation, data processing, anything whose inputs or outputs you might later need to trace. Not for trivial commands (ls, cd, git status).

## Prerequisite

A run started with `provenance-init`. Pass its run id (printed by init, e.g. `run_20260830_142635_c01d7d72`) to `--run`.

## Runtime

`../lib/provenance.py` (stdlib-only). Copy to the compute host once if working over SSH (see `provenance-init`).

## Usage

```bash
python3 provenance.py exec \
  --run run_20260830_142635_c01d7d72 \
  --name "QC clustering" \
  --cmd "Rscript QC_clustering.R" \
  --inputs data/seurat.rds \
  --outputs figs/,tables/markers.csv \
  --env R_process7 \
  --seed 42 \
  --cwd /path/to/workdir \
  [--copy QC_clustering.R]   # copy an UNVERSIONED script into the run's code/
```

## What it does

1. Hashes each `--inputs` path (SHA-256; directories hashed deterministically).
2. Records `execution_started` in the event log.
3. Runs `--cmd` in `--cwd`, capturing stdout/stderr to `logs/<exec_id>.stdout.log` / `.stderr.log`.
4. Hashes each `--outputs` path **after** the command completes (so it records what was actually produced).
5. Records exit code, duration, env, seed, and (if `--copy`) the script hash, then writes the execution record + `execution_finished` event.

**Failed runs are recorded, not discarded.** A non-zero exit code is preserved — a failed experiment is valid provenance.

## Rules

- **Git-anchored code**: if the script is committed to the repo, do NOT use `--copy` — the run's git state (from `provenance-init`) already pins it. `--copy` is only for unversioned/generated scripts.
- **Never** put secrets in `--cmd` or `--name` (persisted in plaintext).
- **Outputs are hashed after the run.** If a command fails before writing an output, the recorded hash may be of a stale pre-existing file — the non-zero exit code flags this; `provenance-verify --rerun` will catch it.
- **Slurm**: record the `sbatch` submission command. The `sbatch` exit code is not the job's exit code — verify outputs after the job finishes with `provenance-verify --rerun`.
- Prefer relative `--inputs`/`--outputs` paths so the run is relocatable; the runtime resolves them against `--cwd`/cwd.
