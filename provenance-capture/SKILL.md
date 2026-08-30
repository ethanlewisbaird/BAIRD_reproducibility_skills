---
name: provenance-capture
description: Capture environment/reference state into a provenance run by detecting what's actually present and choosing the right method. Methods: git, conda (also mamba/micromamba), pixi, docker, renv, pip, rsession (R sessionInfo), sacct (slurm), file (hash/copy a file). The LLM detects the environment, decides which method(s) to use, records a DECISION event explaining the choice, then invokes the runtime's capture command. Use after provenance-init and whenever the execution environment changes.
---

# provenance-capture

Records the environment/state that an execution depends on. This is where the **LLM-as-decision-layer** design pays off: instead of a hardcoded detection chain, the agent detects what's present, decides what to capture, records the decision, and the runtime executes deterministically.

## When to use

- After `provenance-init` — to pin the environment(s).
- When the execution environment changes (new conda env, upgraded R, container swap).
- Before `provenance-report` — so the reproducibility appendix has current pins.
- When adopting pre-existing work (`provenance-adopt`) — to capture the env as of adoption.

## Open method registry

| method | captures | confidence | notes |
|--------|----------|------------|-------|
| `git` | commit, branch, dirty-tree hash | high | always useful |
| `conda` | `conda/mamba/micromamba env export` | high | pass `env=<name>` |
| `pixi` | `pixi.lock`/`pixi.toml` copy, else `pixi export` | high | best pin is the lockfile |
| `docker` | `docker image inspect` → digest | high | pass `image=<name>` |
| `renv` | `renv.lock` copy | high | R package pin |
| `pip` | `pip freeze` | medium | Python fallback |
| `rsession` | `R script -e 'sessionInfo()'` | high | pass `rscript=<path>` |
| `sacct` | slurm `sacct` job state/exit | high | pass `job_id=<id>` |
| `file` | hash + optional copy of one file/dir | high | pass `path=<p>`, `copy=1` |

The registry is **open**: for a genuinely novel case, the LLM composes primitives (e.g. hash the lockfile + `pip freeze`) and records a `decision` explaining the choice. `confidence` labels the evidence strength — the report shows it so nobody mistakes a medium-confidence capture for a high-confidence one.

## Procedure (LLM-driven detection)

### 1. Detect what's present
```bash
which conda mamba micromamba pixi docker Rscript 2>/dev/null
ls pixi.lock pixi.toml renv.lock environment.yml requirements.txt Dockerfile 2>/dev/null
```

### 2. Decide the method(s) and record the decision
```bash
python3 provenance.py record-decision --run <run> \
  --situation "package manager detection" \
  --choice "conda env export (R_process7)" \
  --reason "conda on PATH, env R_process7 exists, no pixi/docker" \
  --confidence high
```

### 3. Capture
```bash
python3 provenance.py capture --run <run> --method conda --kwargs env=R_process7
python3 provenance.py capture --run <run> --method rsession --kwargs rscript=/path/to/Rscript
python3 provenance.py capture --run <run> --method git
```

Each capture writes a record to the manifest (`environment.captures`) and an event to the log, tagged with method + confidence + SHA-256. If a method fails (e.g. no pixi present), it records an honest `error` — the verdict in `provenance-verify` reflects the gap.

## Rules

- Capture the **most specific, most reproducible** pin available: a lockfile (`pixi.lock`, `renv.lock`, conda env export) beats `pip freeze` beats nothing.
- For hybrid R+Python envs (e.g. R + reticulate + MAGIC), capture **both** the R sessionInfo and the Python side.
- **Never** record secrets, API keys, or credentials.
- If the environment can't be captured (no manager present), record the `error` — an honest gap is better than a silent one.