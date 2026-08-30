---
name: provenance-adopt
description: Record scientific work that happened OUTSIDE the provenance system — a human ran a script by hand, made a figure in a GUI, downloaded data, or another tool produced artifacts. The LLM detects/reconstructs what happened, records decisions about how, and wraps the evidence (hashes, history files, asserted lineage) with an ADOPTED or INFERRED evidence label. Use when integrating pre-existing or non-agentic work into a run, so it becomes traceable and verifiable.
---

# provenance-adopt

Captures work that was NOT observed by the provenance system, after the fact. This is fundamentally different from `provenance-exec` (which wraps a run live):

| | `exec` (observed) | `adopt` (post-hoc) |
|---|---|---|
| artifact hashes | yes | yes |
| git state | at run time | as of adoption (labeled) |
| env | at run time | as of adoption (labeled) |
| command/script | known | recoverable (history) or supplied |
| exit code | known | **unknown** (recorded, never guessed) |
| inputs | known | inferred from script or asserted |
| evidence label | `observed` | `adopted` or `inferred` |

**Never guess an exit code.** An adopted run has unknown exit status — record it as unknown. The strongest evidence available is `verify --rerun`: if the adopted script re-runs and reproduces its outputs, that's a `REPRODUCTION_VERIFIED` signal.

## When to use

- A human ran scripts/analysis outside the agent (terminal, GUI, slurm shell).
- Pre-existing results/figures exist that were never provenance-tracked.
- Another tool (not the agent) produced artifacts you need to integrate.
- You have `.bash_history`, `.radian_history`, slurm logs, or file mtimes as evidence.

This maps directly to reconstructing scripts from `.radian_history` — that is a textbook `adopt` operation.

## Procedure (LLM-driven)

The LLM does the interpretation; the runtime (`../lib/provenance.py`) does the deterministic recording.

### 1. Discover the work
- Ask the human what they did, or look for evidence: `.bash_history`, `.radian_history`, slurm `logs/`, `.out`/`.err` files, recent file mtimes, `git reflog`.
- Record a decision about what you found:
```bash
python3 provenance.py record-decision --run <run> \
  --situation "discover pre-existing work" \
  --choice "found .radian_history evidence for QC/clustering" \
  --reason "session history lists QC + clustering commands with timestamps" \
  --confidence medium \
  --evidence ".radian_history"
```

### 2. Reconstruct the commands (if possible)
From history/logs, identify the commands and their order. Record them as the `--cmd` in `exec` with `--evidence-label adopted`. If you can't determine actual commands, note it and use `inferred`.

### 3. Hash the artifacts
The runtime hashes inputs/outputs deterministically:
```bash
python3 provenance.py exec --run <run> \
  --name "human-made figure" \
  --cmd "the reconstructed command" \
  --inputs data/seurat.rds \
  --outputs figs/UMAP.jpeg \
  --evidence-label adopted \
  --notes "reconstructed from .radian_history; exit code unknown"
```

For an **asserted** lineage (human says "this figure came from script Y" but nothing was observed), use `--evidence-label inferred`. This is the weakest evidence — always prefer `adopted` when there's any evidence, and `inferred` only when lineage is purely asserted.

### 4. Record the exit-code uncertainty
Adopted runs have unknown exit codes. The runtime records what it knows and marks the rest. In the manifest, the execution has `evidence_label: adopted` and its exit code reflects the override or is flagged unknown; the report shows the label so nobody mistakes it for an observed run.

### 5. Verify
`verify --rerun` smoke-tests the adopted script — this is exactly Tier-0 smoke testing as a provenance operation. If it reproduces outputs, you get `REPRODUCTION_VERIFIED` (or `VERIFIED_WITH_CAVEAT`). That's the strongest post-hoc signal available.

## Rules

- **Never** fabricate an exit code or claim a run was observed when it wasn't.
- **Always** use `--evidence-label adopted` (recovered) or `inferred` (asserted) — never `observed` — for post-hoc captures.
- **Asserted lineage** (`inferred`) is allowed (it's how human science works — "I remember I made this with that script") but it is visibly weaker and never satisfies `verify` on its own.
- Record decisions about *how* you adopted (what evidence, why you trust it) — that's the audit trail for the edge-case handling itself.
- Secrets and credentials are never recorded, even in notes.
