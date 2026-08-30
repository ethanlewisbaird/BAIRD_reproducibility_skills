---
name: provenance-verify
description: Verify a provenance run — check that every recorded artifact still exists and matches its SHA-256 hash (integrity), and optionally re-run recorded executions to compare output hashes (reproduction). Use before submission, when results seem to have drifted, or after a slurm job completes. Produces PROVENANCE_COMPLETE / PROVENANCE_INCOMPLETE and REPRODUCTION_VERIFIED / REPRODUCTION_DIFFERS verdicts.
---

# provenance-verify

Checks that a run's recorded provenance still holds. This is the "is it reproducible?" step — the one that turns a log into a guarantee.

## When to use

- Before paper submission / sharing results.
- When results seem to have drifted (a figure changed, a dataset was edited).
- After a slurm job completes (to verify the outputs actually match what was recorded at submission time).
- As part of Tier-0 smoke testing: run `--rerun` on a subsample to confirm scripts still execute.

## Runtime

`../lib/provenance.py` (stdlib-only). Copy to the compute host once if working over SSH.

## Usage

```bash
# Integrity check (fast, no re-execution):
python3 provenance.py verify --run run_20260830_142635_c01d7d72

# Full reproduction check (re-runs every recorded command and compares output hashes):
python3 provenance.py verify --run run_20260830_142635_c01d7d72 --rerun --timeout 600
```

## What it checks

**Integrity** — for every artifact recorded in the manifest:
- `OK` — file/dir exists and SHA-256 matches.
- `HASH_MISMATCH` — exists but content changed since recording.
- `MISSING` — gone.

Also requires git state + at least one env/R record present to be `PROVENANCE_COMPLETE` (an honest verdict: if nothing was captured, provenance is incomplete).

**Reproduction** (`--rerun`) — for each recorded execution:
- Re-runs the recorded command in its recorded cwd.
- Compares the re-run exit code and re-hashed outputs against the recorded values.
- `REPRODUCTION_VERIFIED` — all match. `REPRODUCTION_DIFFERS` — any mismatch or failure.

## Verdicts

| verdict | meaning |
|---------|---------|
| `PROVENANCE_COMPLETE` | all artifacts intact + git/env records present |
| `PROVENANCE_INCOMPLETE` | something missing, changed, or never captured |
| `REPRODUCTION_VERIFIED` | re-run produced identical output hashes |
| `REPRODUCTION_DIFFERS` | re-run differed (non-deterministic code, drift, or broken script) |

`REPRODUCTION_DIFFERS` is not necessarily a bug — it flags non-determinism (e.g. unseeded UMAP coordinates) that you should document. Distinguish "provenance complete" (records exist) from "reproduction verified" (re-run matches) — they are different claims.

## Rules

- Verdicts are written to the manifest and event log — they become part of the record.
- Exit code is 0 only if integrity is `PROVENANCE_COMPLETE`; a `--rerun` mismatch does not change the exit code (it's recorded, not fatal).
- For large/slow pipelines, prefer `verify` (integrity) on the full run and `--rerun` on a subsample or a single execution.
