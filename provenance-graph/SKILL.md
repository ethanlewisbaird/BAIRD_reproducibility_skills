---
name: provenance-graph
description: Render the reproducibility graph of a provenance run as Mermaid (embeds in markdown/paper appendix), Graphviz DOT (publication SVG), self-contained HTML (interactive, hover for sha256/exit/cmd), ASCII (terminal over SSH), or JSON (canonical export for networkx/Cytoscape/Gephi). The graph shows run → executions → artifacts (inputs/outputs by SHA-256), environment captures, LLM decisions, and the implicit execution-dependency pipeline (output hash of one = input hash of the next). Use to visualize lineage for reviewers, debugging, or paper figures.
---

# provenance-graph

Renders the lineage graph encoded in a run's manifest + event log. The runtime extracts the graph deterministically; the LLM decides which format fits the audience.

## Usage

```bash
python3 provenance.py graph --run run_20260830_142635_c01d7d72 --format mermaid
# formats: mermaid | dot | html | ascii | json   (default: mermaid)
```

Writes `graph.<ext>` into the run dir (`graph.mmd`, `graph.dot`, `graph.html`, `graph.txt`, `graph.json`) and records a `graph_rendered` event.

## What the graph contains

| node kind | color | shows |
|--------|-------|-------|
| `run` | purple | run id, seed, question |
| `execution` | blue | name, exit code, evidence label, command |
| `artifact` | green | path + first 12 chars of sha256 (full hash on hover) |
| `environment` | orange | capture method, confidence, ERROR if capture failed |
| `decision` | red | LLM decision id + confidence |

Edges: `contains` (run→exec), `produces` (exec→output), `consumed-by` (input→exec), `env` (exec→env by name), `motivated` (decision→exec), and the implicit **`depends-on`** pipeline edges computed by hash matching (output sha256 of one exec = input sha256 of another).

## Choosing a format (LLM decides)

| format | audience | when |
|-------|----------|------|
| `mermaid` | paper appendix, README, GitHub | markdown-renderable, zero deps — **default** |
| `dot` | publication figure | render with graphviz → SVG/PNG (e.g. `dot -Tsvg graph.dot > graph.svg`) |
| `html` | reviewers, debugging | one self-contained file, any browser — **interactive viewer** (see below) |
| `ascii` | terminal over SSH | quick check alongside `verify`/`check` |
| `json` | heavy exploration | canonical export for networkx / Cytoscape / Gephi / neo4j |

Record the format choice as a `decision` event (consistent with the LLM-decides architecture): paper appendix → mermaid (+dot for the figure); debugging → html; quick check → ascii.

## The interactive HTML viewer (designed for scale)

The HTML is a **data/view split**: the graph is embedded as JSON, and a small vanilla-JS viewer renders it client-side. It is built for large analyses — you never stare at the whole graph, you *navigate* it.

- **View modes** — `Pipeline` (executions + `depends-on` edges only: the readable core), `Full` (everything), and **Focus** (click any node → its 1-hop ego graph: what it consumed, what it produced).
- **Pan/zoom** — drag to pan, scroll to zoom (zoom centered on cursor).
- **Search** — type-ahead filter over name/path/cmd; matching nodes render, others hide.
- **Filters** — toggle execution/artifact/env/decision node types; **failures only** (executions with non-zero exit).
- **Detail panel** — click a node → side panel with full metadata (sha256, command, env, seed, exit, evidence label, timestamps).
- **Readability** — artifact labels are basenames (full path on hover/detail); failed executions get a red border; env/decision nodes hidden by default (they're annotations, not pipeline).
- **Readable default zoom** — small graphs fit-all; large graphs land zoomed-in on a window (pan to explore) instead of a hairball.
- **Scale** — verified with a 200-execution / 400-artifact run: pipeline view renders in ~0.1s, DOM ~300KB, and focus-mode keeps any single view small.

## Rules

- The runtime is stdlib-only — it writes **text** (Mermaid/DOT/HTML/JSON/ASCII); rendering happens in the viewer (GitHub, browser, graphviz). No matplotlib/networkx/graphviz dependency in the runtime.
- `graph` is read-only on provenance (writes only its own `graph.<ext>` + a `graph_rendered` event).
- For a paper: paste the mermaid block into the appendix, and render `dot` to SVG for the figure. The JSON export is the canonical machine-readable version.
- The `depends-on` edges are the real pipeline — if they look wrong, check that inputs/outputs were recorded with matching paths — hash matching is exact, so a mismatch means a file genuinely differs.