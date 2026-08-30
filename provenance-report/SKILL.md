---
name: provenance-report
description: Generate a Markdown reproducibility appendix from a provenance run — a figure → script → env → data → seed map, execution table, git state, environment pins, and PROVENANCE_COMPLETE vs REPRODUCTION_VERIFIED verdicts. Use to produce the reproducibility section of a paper, for reviewers, or to audit what produced a given figure.
---

# provenance-report

Turns a provenance run into a human-readable reproducibility appendix. This is the "show the provenance" step — the deliverable a journal or reviewer wants.

## When to use

- Producing the reproducibility section / appendix for a paper.
- Answering "what produced Figure 1?" or "what changed between run A and run B?"
- Auditing a run before sharing it.

## Runtime

`../lib/provenance.py` (stdlib-only). Copy to the compute host once if working over SSH.

## Usage

```bash
python3 provenance.py report --run run_20260830_142635_c01d7d72
# optionally:
python3 provenance.py report --run run_20260830_142635_c01d7d72 --out /path/to/appendix.md
```

Writes `reproducibility_appendix.md` inside the run dir (or to `--out`).

## What it produces

- **Run metadata**: id, created-at, host/user, project, research question, seed.
- **Git state**: commit, branch, dirty-tree hash. This is the code-provenance anchor — a committed script is pinned by the commit; an uncommitted one is pinned by the dirty-tree hash.
- **Environment**: conda env exports + R sessionInfo files with their SHA-256s (the pin files).
- **Executions table**: exec id → exit code → env → seed → command → inputs → outputs.
- **Figure → Script → Env → Data → Seed map**: every figure output (png/jpeg/pdf/svg/tiff) mapped to its producing script (or command), env, seed, and input data. This is the table reviewers want.
- **Verdicts**: integrity + reproduction, from `provenance-verify`.

## Rules

- Run `provenance-verify` before `report` so the verdicts are current; the report reflects whatever is in the manifest.
- The report is derived from the manifest + event log — it is not a separate source of truth.
- For a paper, paste the Executions + Figure map + Verdicts sections into the reproducibility appendix; the full run dir (manifest, event log, env pins, logs) is the supporting artifact you can point reviewers to.
