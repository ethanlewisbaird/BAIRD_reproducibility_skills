# Provenance skills for agentic bioinformatics

Five composable agent skills that record, verify, and report scientific provenance for agent-driven bioinformatics work. Designed for pi (Agent Skills standard), but the runtime is a standalone Python script usable by any agent harness or directly from the shell.

## The five skills

| skill | lifecycle step | what it does |
|-------|---------------|--------------|
| `provenance-init` | start a run | create `results/run_<ts>_<id>/` with manifest skeleton, event log, git commit + dirty-tree hash |
| `provenance-capture` | pin the environment | LLM detects what's present (conda/pixi/docker/renv/pip/R/slurm) and captures the most reproducible pin |
| `provenance-exec` | wrap an execution | hash inputs → run command (stdout/stderr to logs) → hash outputs → record exit code, env, seed, evidence label |
| `provenance-adopt` | integrate non-agentic work | record human/GUI/slurm work after the fact with `adopted`/`inferred` labels |
| `provenance-verify` | check it | integrity check (artifacts still match hashes) + optional `--rerun` (re-run and compare output hashes) |
| `provenance-report` | explain it | emit a Markdown reproducibility appendix: figure → script → env → data → seed map, decisions, verdicts |

## Architecture: LLM decides, runtime executes

The key design decision: **the LLM is the decision layer, the runtime is the execution layer.**

- **LLM (pi agent + skills)** — detects the environment, decides what to capture, decides what's an input/output, authors capture plans and records `decision` events. This is how edge cases are handled: a novel package manager, a hybrid R+Python env, a post-hoc adoption — each becomes a *decision*, not a hardcoded code path.
- **Runtime (`lib/provenance.py`)** — deterministic, stdlib-only, zero dependencies. Executes plans verbatim: hashing, capturing, recording, verifying, reporting. The LLM never computes hashes or writes JSON itself.

Because the LLM's decisions are recorded as first-class `decision` events, the edge-case handling is itself auditable — you can't replay an LLM's reasoning, but you can see what it decided and why.

## Design rules

- **Path = location, SHA-256 = identity.** Artifacts are referenced by content hash, never by path alone.
- **Git is the code-provenance layer.** A committed script is pinned by commit + dirty-tree hash; `--copy` into the run's `code/` is only for unversioned scripts. No second source of truth.
- **Graph vs. store.** Large artifacts stay in place on disk; the manifest references them by hash. The manifest is an index, not a warehouse.
- **Event log is truth; manifest is derived.** `provenance.jsonl` is append-only and survives crashes; `manifest.json` is a materialized summary updated on each event.
- **Failed runs are recorded.** Non-zero exit codes and errors are preserved — failure is valid provenance.
- **Verification is first-class.** `verify` distinguishes *provenance complete* (records intact) from *reproduction verified* (re-run matches). They are different claims.
- **Evidence labels.** Every execution is labeled `observed` / `adopted` / `inferred` — the report shows the strength of each piece of evidence.
- **No secrets.** Credentials and API keys are never recorded.
- **Zero dependencies.** `lib/provenance.py` is stdlib-only; works locally or over SSH (scp once, then call remotely).
- **Full SHA-256 always** (streaming, 1 MiB chunks) — no fast mode, even for huge files.

## Run layout

```
results/run_<YYYYMMDD_HHMMSS>_<id>/
├── manifest.json              # materialized summary (updated on each event)
├── provenance.jsonl           # append-only event log (source of truth)
├── environment/               # capture records (conda/pixi/docker/renv/pip/R/sacct/git/file)
├── executions/                # per-execution JSON records
├── logs/                      # <exec_id>.stdout.log / .stderr.log
├── code/                      # optional copies of unversioned scripts
└── outputs/                   # optional output copies
```

## Typical flow

```bash
# 1. start the run (captures git state + metadata)
provenance.py init --project . --question "DE analysis" --seed 42
# → results/run_20260830_142635_c01d7d72

# 2. LLM decides + captures the environment
provenance.py record-decision --run <run> --situation "pkg manager" --choice "conda" --reason "conda on PATH, env exists" --confidence high
provenance.py capture --run <run> --method conda --kwargs env=R_process7
provenance.py capture --run <run> --method rsession --kwargs rscript=/path/to/Rscript

# 3. wrap each execution
provenance.py exec --run <run> --name "QC" \
  --cmd "Rscript QC_clustering.R" --inputs data/seurat.rds --outputs figs/ --env R_process7 --seed 42

# 4. integrate non-agentic work (if any)
provenance.py exec --run <run> --name "hand-made figure" --cmd "..." --evidence-label adopted

# 5. verify (before submission) — this is the smoke test
provenance.py verify --run <run> --rerun

# 6. report the appendix
provenance.py report --run <run>
```

## Verdicts

- `PROVENANCE_COMPLETE` / `PROVENANCE_INCOMPLETE` — do the records hold?
- `REPRODUCTION_VERIFIED` / `VERIFIED_WITH_CAVEAT` / `REPRODUCTION_DIFFERS` — does a re-run match?

`REPRODUCTION_DIFFERS` often reflects non-determinism (e.g. unseeded UMAP) that must be *documented*, not an error. Mark such executions `--expected-nondeterministic` to get `VERIFIED_WITH_CAVEAT` instead.

## Scope / limitations

- Level 2 maturity (wrapper records executions); not a sandbox that auto-captures everything. The agent must invoke `exec` around consequential commands.
- No graph database — lineage is traversable from the manifest/event log; designed to be exported later.
- Agent decision/context capture (the LLM's full reasoning) is deliberately out of scope: the record holds compact metadata + hashes + recorded decisions, never full agent context or secrets.
- Env capture is best-effort where package managers are unavailable — an honest `error` record is better than a silent gap.
