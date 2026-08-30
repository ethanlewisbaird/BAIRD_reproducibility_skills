# Provenance skills for agentic bioinformatics

Four composable agent skills that record, verify, and report scientific provenance for agent-driven bioinformatics work. Designed for pi (Agent Skills standard), but the runtime is a standalone Python script usable by any agent harness or directly from the shell.

## The four skills

| skill | lifecycle step | what it does |
|-------|---------------|--------------|
| `provenance-init` | start a run | create `results/run_<ts>_<id>/` with manifest skeleton, event log, git commit + dirty-tree hash, conda env exports, R sessionInfo |
| `provenance-exec` | wrap an execution | hash inputs → run command (stdout/stderr to logs) → hash outputs → record exit code, env, seed, script hash |
| `provenance-verify` | check it | integrity check (artifacts still match hashes) + optional `--rerun` (re-run and compare output hashes) |
| `provenance-report` | explain it | emit a Markdown reproducibility appendix: figure → script → env → data → seed map, verdicts |

## Design rules

- **Path = location, SHA-256 = identity.** Artifacts are referenced by content hash, never by path alone.
- **Git is the code-provenance layer.** A committed script is pinned by commit + dirty-tree hash; `--copy` into the run's `code/` is only for unversioned scripts. No second source of truth.
- **Graph vs. store.** Large artifacts stay in place on disk; the manifest references them by hash. The manifest is an index, not a warehouse.
- **Event log is truth; manifest is derived.** `provenance.jsonl` is append-only and survives crashes; `manifest.json` is a materialized summary updated on each event.
- **Failed runs are recorded.** Non-zero exit codes and errors are preserved — failure is valid provenance.
- **Verification is first-class.** `verify` distinguishes *provenance complete* (records intact) from *reproduction verified* (re-run matches). They are different claims.
- **No secrets.** Credentials and API keys are never recorded.
- **Zero dependencies.** `lib/provenance.py` is stdlib-only; works locally or over SSH (scp once, then call remotely).

## Run layout

```
results/run_<YYYYMMDD_HHMMSS>_<id>/
├── manifest.json              # materialized summary (updated on each event)
├── provenance.jsonl           # append-only event log (source of truth)
├── environment/               # git_state.txt, env_<name>.yml, sessionInfo.txt
├── executions/                # per-execution JSON records
├── logs/                      # <exec_id>.stdout.log / .stderr.log
├── code/                      # optional copies of unversioned scripts
└── outputs/                   # optional output copies
```

## Typical flow

```bash
# 1. start the run (captures git, envs, R)
provenance.py init --project . --question "DE analysis" --seed 42 --env R_process7 --rscript /path/to/Rscript
# → results/run_20260830_142635_c01d7d72

# 2. wrap each execution
provenance.py exec --run run_20260830_142635_c01d7d72 --name "QC" \
  --cmd "Rscript QC_clustering.R" --inputs data/seurat.rds --outputs figs/ --env R_process7 --seed 42

# 3. verify (before submission)
provenance.py verify --run run_20260830_142635_c01d7d72 --rerun

# 4. report the appendix
provenance.py report --run run_20260830_142635_c01d7d72
```

## Verdicts

- `PROVENANCE_COMPLETE` / `PROVENANCE_INCOMPLETE` — do the records hold?
- `REPRODUCTION_VERIFIED` / `REPRODUCTION_DIFFERS` — does a re-run match?

`REPRODUCTION_DIFFERS` often reflects non-determinism (e.g. unseeded UMAP) that must be *documented*, not an error.

## Scope / limitations

- Level 2 maturity (wrapper records executions); not a sandbox that auto-captures everything. The agent must invoke `exec` around consequential commands.
- No graph database — lineage is traversable from the manifest/event log; designed to be exported later.
- Agent decision/context capture (LLM layer) is deliberately out of scope: the record holds compact metadata + hashes, never full agent context.
- Env capture is best-effort where conda/R are unavailable.
