# scigraph

Lazily constructed, provenance-preserving evidence subgraphs of the scientific
literature, built by a code agent in answer to a research question and then
pooled.

The division of labour:

- **The agent** (Claude Code, via the `evidence-subgraph` skill) reads papers and
  makes the judgements: what was the experiment, what was observed, what was
  merely interpreted, what bears on which hypothesis, and how confidently.
- **The `scigraph` CLI** does everything deterministic, and calls no model:
  OpenAlex retrieval, schema validation, checking that every quoted passage is
  verbatim in the source text, storage, linting, and pooling.

No API keys. The agent harness is the LLM.

## Setup

```sh
uv tool install --editable .     # puts `scigraph` on PATH
```

The skill lives in `skills/evidence-subgraph/`. Inside this repo Claude Code
finds it via `.claude/skills`; elsewhere, load the repo as a plugin
(`claude --plugin-dir /path/to/llm-sci-graph`). Then ask a question:

> /evidence-subgraph why is the ego-depletion effect still disputed?

## Commands

| | |
|---|---|
| `scigraph search "<query>"` | search OpenAlex, cache papers with abstracts |
| `scigraph paper show\|add\|text` | read a cached paper; add a non-OpenAlex source; attach full text |
| `scigraph new <slug> --question "..."` | start a subgraph |
| `scigraph add <slug> batch.json` | validate a batch of nodes and edges and apply it (all-or-nothing) |
| `scigraph show <slug> [--format summary\|json\|mermaid]` | |
| `scigraph lint <slug>` | structural gaps: experiments without conditions, unevidenced hypotheses, no recorded failures, unverified passages |
| `scigraph pool <slug>` / `scigraph pool --list` | push to the pool |
| `scigraph view [slug ...]` | browse subgraphs in the browser (all local ones by default) |

Data lives in `$SCIGRAPH_HOME` (default `~/.local/share/scigraph`): `papers/`,
`fulltext/`, `subgraphs/<slug>.json`, `pool.sqlite`.

## Browsing

`scigraph view` serves a single page on `http://127.0.0.1:8765/` and opens it;
refresh to pick up new data while a subgraph is being built. (`-o file.html`
writes the same page as a self-contained file instead.) Click a node or edge for its verbatim
passages, confidence, who asserted it and any extractor note; the default panel
lists each hypothesis with its evidence for and against, and results in direct
conflict. Filter by node type, relation, asserter and minimum confidence; a table
view lists the same edges. "All subgraphs together" also draws dotted links
between nodes that were independently given the same id: the cheapest alignment
candidates. The URL hash records the subgraph and selection, so a view can be
bookmarked. The graph library (Cytoscape.js) loads from a CDN; offline, the table
and detail panel still work.

## Model

Nodes: `hypothesis`, `experiment`, `condition`, `observation` (with an outcome:
positive / negative / null / inconclusive / mixed), `interpretation`.
Edges: `tests`, `performed_under`, `yields`, `observed_under`, `supports`, `contradicts`,
`explains`, `proposes`, `competes_with`, `fails_to_replicate`, each restricted to
sensible endpoint types (`src/scigraph/models.py`).

Every edge carries a confidence, who asserted it (the paper's `author`s or the
extracting `model`), and a verbatim passage from exactly one paper. The same
relation evidenced by another paper is another edge, so evidence accumulates
rather than overwriting. A passage that does not appear in the held source text
is rejected; one that cannot be checked (no text held) is kept as
`verified: null`.

## Pooling

The pool is local SQLite for now. Pooled subgraphs stay separate, with node ids
namespaced by slug; alignment will add links between them rather than merge
them. Setting `SCIGRAPH_POOL_URL` switches to a remote pool
(`POST {url}/subgraphs`, `GET {url}/subgraphs`), so a server can arrive later
without the skill changing.

## Not yet

- Alignment: cheap candidate collisions across pooled subgraphs, then agent
  judgement spent only where a match would change the graph's structure.
- Observation: structural queries over the aligned pool (failures sharing a
  condition, contradictions that separate by regime, thinly evidenced
  hypotheses).
- The pool server.

## Development

```sh
uv run pytest
```
