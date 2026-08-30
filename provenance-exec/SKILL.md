---
name: provenance-exec
description: Wrap a single computation with provenance capture — hash inputs, run the command capturing stdout/stderr, hash outputs, record exit code, env, seed, evidence label, and git state. Use for every scientifically consequential execution (analysis scripts, figure generation, data processing) inside a run started by provenance-init. The LLM decides what is an input/output and how to label it; the runtime executes deterministically. Works locally or over SSH.
---

# provenance-exec

Records one execution inside a run. This is the "record what happened" step of the provenance workflow.

## When to use

For every scientifically consequential command: analysis scripts, figure generation, data processing, anything whose inputs or outputs you might later need to trace. Not for trivial commands (ls, cd, git status).

## Prerequisite

A run started with `provenance-init`. Pass its run id (printed by init, e.g. `run_20260830_142635_c01d7d72`) to `--run`.

## Architecture

The **LLM decides** what the inputs/outputs are, which env/seed applies, and how to label the execution. The **runtime executes deterministically** — hashing, running, capturing logs, writing JSON. The LLM never computes hashes itself.

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
  --evidence-label observed \
  [--copy QC_clustering.R]   # copy an UNVERSIONED script into the run's code/
```

## Evidence labels

| label | meaning | when |
|-------|---------|------|
| `observed` | agent wrapped the execution | default — the run was live |
| `adopted` | recorded after the fact, exit code may be unknown | human/GUI/slurm did it; reconstructed from history |
| `inferred` | lineage asserted by a human, not observed | weakest — "I remember I made this with that script" |

Use `--evidence-label adopted` or `inferred` for post-hoc captures (see `provenance-adopt`). Never label a post-hoc capture `observed`.

## What it does

1. Hashes each `--inputs` path (SHA-256; directories hashed deterministically).
2. Records `execution_started` in the event log.
3. Runs `--cmd` in `--cwd`, capturing stdout/stderr to `logs/<exec_id>.stdout.log` / `.stderr.log`.
4. Hashes each `--outputs` path **after** the command completes (so it records what was actually produced).
5. Records exit code, duration, env, seed, evidence label, and (if `--copy`) the script hash, then writes the execution record + `execution_finished` event.

**Failed runs are recorded, not discarded.** A non-zero exit code is preserved — a failed experiment is valid provenance.

## Edge-case flags

- `--exit-code-override <n>` — for adopted runs where you know the real exit code (e.g. from a slurm log) without re-running.
- `--expected-nondeterministic` — mark an execution as expected to differ on re-run (e.g. unseeded UMAP). `provenance-verify --rerun` then reports `VERIFIED_WITH_CAVEAT` instead of a false `DIFFERS`.
- `--notes "..."` — free-text context (never secrets).

## Rules

- **Git-anchored code**: if the script is committed to the repo, do NOT use `--copy` — the run's git state (from `provenance-init`) already pins it. `--copy` is only for unversioned/generated scripts.
- **Never** put secrets in `--cmd`, `--name`, or `--notes` (persisted in plaintext).
- **Outputs are hashed after the run.** If a command fails before writing an output, the recorded hash may be of a stale pre-existing file — the non-zero exit code flags this; `provenance-verify --rerun` will catch it.
- **Slurm**: record the `sbatch` submission command. The `sbatch` exit code is not the job's exit code — use `provenance-capture --method sacct --kwargs job_id=<id>` and verify outputs after the job finishes.
- Prefer relative `--inputs`/`--outputs` paths so the run is relocatable; the runtime resolves them against `--cwd`/cwd.