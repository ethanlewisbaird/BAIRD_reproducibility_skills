---
name: provenance-check
description: Audit a provenance run for procedure compliance and discipline — a linter for provenance. Checks init/capture/exec/adopt/verify discipline, evidence-label validity, secret hygiene, output/input hashing, and reproduction handling. Emits PROVENANCE_CHECK_PASS or PROVENANCE_CHECK_FAIL with a per-check severity table. Use before provenance-report or paper submission to catch gaps: mislabeled executions, unhashed outputs, secrets accidentally recorded, missing env capture, undocumented adopted work.
---

# provenance-check

A discipline linter over a run's manifest + event log. It answers: *"did this run follow the provenance procedure, and is it safe to report?"* Run it before `provenance-report` or submission.

## Usage

```bash
python3 provenance.py check --run run_20260830_142635_c01d7d72
```

Writes `provenance_check.md` into the run dir, prints the checklist, records a `check_completed` event + verdict in the manifest. Exit code: `0` = PASS (no FAILs), `1` = any FAIL.

## What it checks

| severity | check | what it means |
|----------|-------|---------------|
| FAIL | init: run_started event present | `provenance-init` was called |
| WARN | git state captured | run is in a git repo (not fatal — some projects aren't) |
| WARN | environment captured (or honest error) | at least one `capture` succeeded, or an honest error was recorded |
| FAIL | all executions have valid evidence labels | every exec has `observed`/`adopted`/`inferred` |
| FAIL | all executions have a recorded command | no empty `--cmd` |
| WARN | all outputs hashed | every output recorded with a SHA-256 |
| WARN | all inputs hashed | every input recorded with a SHA-256 |
| WARN | adopted/inferred executions documented | weak-evidence execs have `--notes` or a decision |
| FAIL | no secrets recorded | scans manifest + event log + captured stdout/stderr for API keys, passwords, tokens, bearer/auth headers, private keys |
| WARN | verify recorded before report | report exists but no verify event |
| WARN | reproduction differences addressed | if reproduction = DIFFERS, are nondeterministic execs marked? |

## Verdicts

- `PROVENANCE_CHECK_PASS` — no FAILs (warnings are acceptable, honest gaps).
- `PROVENANCE_CHECK_FAIL` — at least one FAIL (procedure violation or secret leak). Do not report until resolved.

## Interpreting severity

- **FAIL** = a violation that undermines the provenance claim (mislabeled exec, secret recorded, missing init). Fix before reporting.
- **WARN** = an honest gap or unaddressed risk (no git repo, no env capture, reproduction differs). Warnings are *expected* in many real runs — the linter is honest about them, and the report shows them so a reviewer sees the gap.

## Remediation for common failures

- **Secret recorded** → find it in the run (manifest/events/logs), remove it, and re-capture. Never report a run with a leaked secret.
- **Mislabeled execution** → re-`exec` with the correct `--evidence-label`; never relabel a post-hoc capture as `observed`.
- **Missing env capture** → `provenance-capture` with the right method (conda/pixi/docker/renv/pip/rsession).
- **Unhashed outputs** → re-`exec` with the output paths so they're hashed after the run.
- **Undocumented adopted/inferred** → add `--notes` or a `record-decision` explaining the evidence.
- **Reproduction differs** → mark nondeterministic execs `--expected-nondeterministic`, or investigate the drift.

## Rules

- `check` is read-only on the run's provenance (it writes only its own `provenance_check.md` + a `check_completed` event).
- Run `check` **after** `provenance-verify` and **before** `provenance-report` — the report should reflect a checked run.
- The secret scan is heuristic (pattern-based) — it catches common shapes but is not a guarantee. Treat it as a first-pass hygiene gate.
- Warnings are not failures: a run can be `PROVENANCE_CHECK_PASS` with warnings, and that's honest. Do not silently "fix" warnings by deleting evidence — re-capture properly instead.