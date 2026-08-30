---
name: provenance-report
description: Generate a Markdown reproducibility appendix from a provenance run — a figure → script → env → data → seed map, execution table with evidence labels (observed/adopted/inferred), environment captures, git state, LLM decisions (edge-case handling), and verdicts. Use to produce the reproducibility section of a paper, for reviewers, or to audit what produced a given figure.
---

# provenance-report

Turns a provenance run into a human-readable reproducibility appendix. This is the "show the provenance" step — the deliverable a journal or reviewer wants.

## Usage

```bash
python3 provenance.py report --run run_20260830_142635_c01d7d72
# optionally:
python3 provenance.py report --run run_20260830_142635_c01d7d72 --out /path/to/appendix.md
```

Writes `reproducibility_appendix.md` inside the run dir (or to `--out`).

## What it produces

- **Run metadata**: id, created-at, host/user, project, research question, seed.
- **Git state**: commit, branch, dirty-tree hash — the code-provenance anchor.
- **Environment**: every capture (method, confidence, detail, sha256) — conda env exports, pixi lockfiles, R sessionInfo, docker digests, etc.
- **Executions table**: exec id → evidence label → exit → env → seed → command → inputs → outputs.
- **Figure → Script → Env → Data → Seed map**: every figure output mapped to its producing script (or command), env, seed, and input data — with the evidence label so readers can see whether each figure was `observed`, `adopted`, or `inferred`.
- **Decisions**: every recorded LLM decision (edge-case handling) — situation, choice, confidence, reason. This is the audit trail for *how* the system chose to capture things.
- **Verdicts**: integrity + reproduction, from `provenance-verify`.

## Evidence labels in the report

The report is honest about evidence strength:
- `observed` — the agent wrapped the run live (strongest).
- `adopted` — recorded after the fact from evidence/history (medium; exit code may be unknown).
- `inferred` — lineage asserted by a human, not observed (weakest).

A reviewer can see at a glance which figures are backed by live observation and which are post-hoc reconstructions.

## Rules

- Run `provenance-verify` before `report` so the verdicts are current; the report reflects whatever is in the manifest.
- The report is derived from the manifest + event log — it is not a separate source of truth.
- For a paper, paste the Executions + Figure map + Decisions + Verdicts sections into the reproducibility appendix; the full run dir (manifest, event log, captures, logs) is the supporting artifact.
- The Decisions section is what makes agentic provenance auditable: it shows not just *what* was captured but *why the system chose that capture method*.