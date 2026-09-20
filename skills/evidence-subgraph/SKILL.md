---
name: evidence-subgraph
description: Build a provenance-rich evidence subgraph from the scientific literature in answer to a research question (hypotheses, experiments, conditions, observations, interpretations, and the evidential relations between them), then pool it. Use when the user asks why a hypothesis is unresolved, what the evidence for or against a claim actually is, where results conflict, what has been tried and failed, or asks to "build a subgraph", "map the evidence", or "pool" an investigation.
---

# Evidence subgraph

You are building a small, inspectable graph of *what was done and what was seen*
in answer to one research question. The unit of knowledge is the experimentally
grounded relationship, not the paper. Papers are sources of evidence about those
relationships.

You do the reading and the judgement. The `subgraft` CLI does the deterministic
parts: retrieval, schema validation, checking that your quoted passages really
appear in the source, storage, and pooling. If `subgraft` is not on PATH, run it
as `uv run --project <repo> subgraft ...`, where `<repo>` is two directories above
this skill's base directory.

The subgraph will later be pooled with subgraphs built by other people asking
other questions, and aligned against them. That is where the value is: a condition
you record carefully may be the link to an investigation you know nothing about.
So record what is there, specifically, rather than what serves the question's
narrative.

## Workflow

1. **Frame.** Restate the question as the hypotheses in contention. Pick a slug.
   `subgraft new <slug> --question "..."`
2. **Retrieve.** Run several searches, not one: the claim itself, its rivals,
   and deliberately the failures (`replication`, `null result`, `failed to`,
   `no effect`, `boundary condition`, `registered report`, `meta-analysis`).
   `subgraft search "<query>" [--limit N] [--from-year Y] [--sort cited_by_count:desc]`
   Results are cached; read one with `subgraft paper show <id>`. Aim for 8-20
   papers that bear directly on the question, including grey literature
   (preprints, dissertations) where it turns up.
3. **Extract, one paper at a time.** Write a JSON batch and apply it:
   `subgraft add <slug> batch.json`. It is all-or-nothing; fix what it reports
   and re-run. Before each paper, `subgraft show <slug>` so you reuse existing
   node ids instead of minting duplicates.
4. **Check.** `subgraft lint <slug>` and act on what it finds. A finding you
   cannot fix from the sources is fine; say so in your summary.
5. **Report.** Summarise what the graph shows: where the evidence sits, where
   it conflicts and under which conditions, what is thinly evidenced, and what
   you could not verify. `subgraft show <slug> --format mermaid` gives a diagram.
6. **Pool.** `subgraft pool <slug>`. With `SUBGRAFT_POOL_URL` unset the pool is
   a local file and you can just do it. If it is set, pooling publishes the
   subgraph to a shared server: ask the user first.

## Node types

| type | what it is | id prefix |
|---|---|---|
| `hypothesis` | A claim about the world that evidence could bear on. | `h:` |
| `experiment` | A specific study, arm or analysis that was actually run. One per study in a multi-study paper. | `e:` |
| `condition` | A circumstance an experiment was run under: population, species, dose, task, temperature, measure, preregistration, sample size band. | `c:` |
| `observation` | What was seen, stated without explanation. Needs an `outcome`: `positive`, `negative`, `null`, `inconclusive`, `mixed` (relative to what the experiment was looking for). | `o:` |
| `interpretation` | An explanation someone offered for an observation. | `i:` |

Ids are lowercase kebab slugs, e.g. `c:sequential-task-paradigm`. Make condition
and hypothesis ids generic and reusable ("hypoxia", not "hypoxia in Smith 2019"),
since they are what different papers, and later different subgraphs, share.
Experiments and observations are particular, so name them after the study
(`e:hagger-2016-multilab`).

## Relations

| relation | from -> to | meaning |
|---|---|---|
| `tests` | experiment -> hypothesis | the experiment was designed to bear on it |
| `performed_under` | experiment -> condition | |
| `yields` | experiment -> observation | |
| `observed_under` | observation -> condition | the result holds for a subgroup, moderator level or regime within the experiment, not the whole of it |
| `supports` / `contradicts` | observation -> hypothesis | evidential bearing |
| `contradicts` | observation -> observation | two results that cannot both hold as stated |
| `explains` | interpretation -> observation | |
| `proposes` | interpretation -> hypothesis | the explanation puts forward a new claim |
| `competes_with` | hypothesis -> hypothesis | rival accounts of the same observations |
| `fails_to_replicate` | experiment -> experiment | |

Every edge needs `confidence` (0-1), `asserted_by`, and provenance from exactly
one paper. The same relation evidenced by a second paper is a second edge: that
is how evidence accumulates, and how a later source weakens an earlier one.

## Rules that matter

**Separate what was seen from what was said about it.** Authors slide between
result and explanation within a sentence. "Depletion reduced persistence,
consistent with a limited resource" is an observation (reduced persistence) plus
an interpretation (limited resource). Split them. The observation `supports` the
hypothesis; the interpretation `explains` the observation.

**`asserted_by` is about who drew the link.** `author` if the paper itself states
the relationship; `model` if you inferred it. A null result whose authors never
mention the hypothesis it undermines is `contradicts`, `asserted_by: model`.
Model-asserted edges are welcome, they are often the interesting ones, but they
must be labelled.

**Confidence is how sure you are the relationship holds as stated, given this
passage** - not how good the paper is, and not how strong the effect was. Rough
guide: 0.9+ the paper says it in so many words; 0.7-0.85 clear from the passage
with little inference; 0.4-0.65 a reasonable reading that another reader might
dispute; below 0.4 probably not worth recording. Keep model-asserted edges
at or below 0.85.

**Passages are verbatim.** Copy the quote exactly from the abstract or attached
text; `...` may elide words within a quote. `subgraft add` rejects a passage that
is not in the text it holds, or that starts or ends mid-word (the mark of a
quote copied from truncated output rather than read; go back to the source for
the whole sentence). When that happens, re-read and fix the quote -
never paraphrase into quotation marks, and never weaken the check by quoting
two words. Quote enough that a reader sees the relationship in the passage.

**Failures are first-class.** Null results, failed replications, abandoned
approaches and "did not reach significance" are constraints on hypothesis space,
and the literature under-reports them. Record them with the same care as
positive results, with their conditions: a failure is only informative alongside
the circumstances it failed under.

**Conditions are the alignment surface.** Record every condition the source
gives you, even ones that seem irrelevant to this question. Put quantities in
`attrs` (`{"n": 2141, "labs": 23, "dose_mg_kg": 5}`) and keep the label
human-readable.

**Do not smooth over disagreement.** If two papers conflict, record both and a
`contradicts` edge between the observations. Do not pick a winner.

## Beyond the abstract

Abstracts omit conditions and nulls. For the papers that matter most, fetch the
open-access full text (the `url` field, or a preprint server), save it as plain
text, and attach it so passages from it can be verified:
`subgraft paper text <id> fulltext.txt`, then cite with `"location": "methods"`
(or `results`, `discussion`). A passage from text that is not attached is
accepted with a warning and stays `verified: null`; keep those few.

For a source OpenAlex does not have, write a paper JSON (`id`, `title`, and what
you know of `doi`, `year`, `authors`, `venue`, `url`, `abstract`, `source_tier`:
`published` | `grey` | `process`) and `subgraft paper add paper.json`.

## Batch format

```json
{
  "nodes": [
    {"id": "h:ego-depletion", "type": "hypothesis",
     "label": "Self-control draws on a limited resource that is depleted by use"},
    {"id": "e:hagger-2016-multilab", "type": "experiment",
     "label": "Hagger et al. 2016 preregistered multilab replication",
     "attrs": {"labs": 23, "n": 2141},
     "provenance": [{"paper_id": "W2499154041", "location": "abstract",
                     "passage": "<verbatim quote describing the study>"}]},
    {"id": "c:preregistered", "type": "condition", "label": "Preregistered protocol"},
    {"id": "o:hagger-2016-near-zero-effect", "type": "observation", "outcome": "null",
     "label": "Depletion effect close to zero across labs"}
  ],
  "edges": [
    {"source": "e:hagger-2016-multilab", "target": "h:ego-depletion",
     "relation": "tests", "confidence": 0.95, "asserted_by": "author",
     "provenance": [{"paper_id": "W2499154041", "location": "abstract",
                     "passage": "<verbatim quote>"}]},
    {"source": "o:hagger-2016-near-zero-effect", "target": "h:ego-depletion",
     "relation": "contradicts", "confidence": 0.7, "asserted_by": "model",
     "note": "Authors frame it as failure to replicate one paradigm, not refutation.",
     "provenance": [{"paper_id": "W2499154041", "location": "abstract",
                     "passage": "<verbatim quote>"}]}
  ]
}
```

Nodes may carry provenance too (useful for experiments and observations); edges
must. Re-adding an existing node id merges provenance; re-adding an edge from the
same paper replaces it, so correcting an extraction is just another `add`.
