---
name: provenance-verify
description: Verify a provenance run's integrity (all captured inputs/outputs still present with matching SHA-256 hashes) and optionally its reproduction (re-run every execution and compare exit codes + output hashes). Produces verdicts: PROVENANCE_COMPLETE/INCOMPLETE and REPRODUCTION_VERIFIED / VERIFIED_WITH_CAVEAT / REPRODUCTION_DIFFERS. Use to validate a run before reporting, or as a smoke test that outputs reproduce.
---

# provenance-verify

Checks that captured provenance is (1) trustworthy (integrity) and (2) reproduces (optional re-run). This maps directly onto Tier-0 smoke testing: `verify --rerun` is the smoke test as a provenance operation.

## Usage

```bash
# Integrity only (fast, no re-run)
python3 provenance.py verify --run run_20260830_142635_c01d7d72

# Integrity + reproduction (re-runs every execution!)
python3 provenance.py verify --run run_20260830_142635_c01d7d72 --rerun [--timeout 300]
```

## Integrity verdicts

| verdict | meaning |
|---------|---------|
| `PROVENANCE_COMPLETE` | every recorded input/output exists and its current SHA-256 matches the recorded hash |
| `PROVENANCE_INCOMPLETE` | something is missing or its hash changed |

**PROVENANCE_INCOMPLETE does not mean the science is wrong** — it means the captured state has drifted (a file was moved/edited, an env changed, a dependency updated). It's a signal to re-capture or investigate, not a scientific verdict.

## Reproduction verdicts (with --rerun)

Each execution is re-run; a match = (exit code matches recorded) AND (output hashes match). Labels are shown per execution: `observed`, `adopted`, `inferred`.

| verdict | meaning |
|---------|---------|
| `REPRODUCTION_VERIFIED` | all reruns matched recorded exit codes + output hashes |
| `VERIFIED_WITH_CAVEAT` | all matched, but at least one execution was `expected_nondeterministic` (e.g. unseeded UMAP) — outputs may shift on re-run BY DESIGN |
| `REPRODUCTION_DIFFERS` | at least one execution's rerun did not match |

**Adopted/inferred executions with unknown recorded exit codes** are treated as matching if their outputs match (re-run success is the strongest signal we have) — but the label makes it visible that this was post-hoc evidence, not observed.

## Tier-0 smoke testing (verify --rerun as the smoke test)

This is the recommended way to smoke-test a pipeline:
1. `provenance-exec` each script with the **real** (or representative) inputs.
2. `verify --rerun` — executes every script again in order, hashes outputs, and reports which reproduce.
3. A `REPRODUCTION_DIFFERS` result with a matching run log tells you *which* script diverged and how — exactly what a smoke test needs.

## Rules

- `--rerun` re-executes **every** recorded command — it can be expensive (GPU jobs, long analyses). Use a short `--timeout` caution and a subsample for smoke tests.
- `--rerun` re-runs in the **current** env, not the recorded env — a `DIFFERS` may be an env drift, not a real reproducibility failure. Pair with `provenance-capture` to diagnose.
- Integrity drift (`PROVENANCE_INCOMPLETE`) is distinct from reproduction failure — report them separately; don't conflate "file moved" with "script broken".
- Never modify the provenance log during verify; verification is read-only except for rerun's own logs (written to the run dir as verify artifacts).