---
name: evidence-subgraph
description: Build a provenance-rich evidence subgraph from the scientific literature in answer to a research question (hypotheses, experiments, conditions, observations, interpretations, and the evidential relations between them), then pool it. Use when the user asks why a hypothesis is unresolved, what the evidence for or against a claim actually is, where results conflict, what has been tried and failed, or asks to "build a subgraph", "map the evidence", or "pool" an investigation.
---

# Evidence subgraph

You are building a small, inspectable graph of *what was done and what was seen* in answer to one research question. The unit of knowledge is the experimentally grounded relationship, not the paper. Papers are sources of evidence about those relationships.

You do the reading and the judgement. The `scibraid` CLI does the deterministic parts: retrieval, schema validation, checking that your quoted passages really appear in the source, storage, and pooling. If `scibraid` is not on PATH, run it as `uv run --project <repo> scibraid ...`, where `<repo>` is two directories above this skill's base directory.

The subgraph will later be pooled with subgraphs built by other people asking other questions, and aligned against them. That is where the value is: a condition you record carefully may be the link to an investigation you know nothing about. So record what is there, specifically, rather than what serves the question's narrative.

## Workflow

1. **Frame.** Restate the question as the hypotheses in contention, including the rivals of whatever the question seems to favour, and pick a slug. Record the framing when you create the subgraph: `scibraid new <slug> --question "..." --hypothesis "..." --hypothesis "..." --condition "..." --brief "..." --model <your model id>`. See Framing below for what belongs in each, and for what to do when the user supplies some of it. The tool records who did the work: the person, the harness and the session come from the environment, but it cannot see which model you are, so pass `--model <your model id>` on `new` and on every `add`. Agreement between subgraphs only means something if the pool can tell whether they were read independently. If the question is a lead's follow-up (`scibraid followups --status pending`), start with `scibraid new <slug> --lead <lead-id>` instead: the question comes from the lead, and the subgraph is seeded with the lead's claim as a hypothesis marked as derived from the pool. Treat that hypothesis as the thing under test, not as a finding. It has no evidence yet, and your job is to find what the literature says for and against it, with the same care for failures as anywhere else. Add the rival hypotheses too, with `scibraid frame <slug> --hypothesis "..."`.
2. **Retrieve.** Run several searches, not one: the claim itself, its rivals, and deliberately the failures (`replication`, `null result`, `failed to`, `no effect`, `boundary condition`, `registered report`, `meta-analysis`). `scibraid search "<query>" [--limit N] [--from-year Y] [--sort cited_by_count:desc]` matches titles and abstracts. Word search misses papers that use other words, so once you have two or three central papers follow their citations as well: `scibraid search --references-of <id>` lists what a paper cites and `scibraid search "<query>" --citing <id>` what cites it, which is also where the failed replications of a well-known result are found. When you already know of a paper, from a reference list, the web or your own knowledge, fetch its record by identifier with `scibraid paper get <DOI | arXiv id or URL | PubMed id | OpenAlex id>`; do not write its metadata by hand, because a hand-made id stops the pool recognising the paper when another subgraph cites it. If it prints a note, read it: for an arXiv id it checks the title against arXiv, and says when it had to find the paper by title or build the record from arXiv's metadata. A subgraph that already cites a hand-added paper is put right with `scibraid paper reid <old id>`. Results are cached; read one with `scibraid paper show <id>`. Each result says whether an open copy exists. A closed paper still counts: extract it from its abstract, and tell the user which closed papers mattered so that they can obtain them. Leaving them out would bias the review towards what happens to be free. Aim for 8-20 papers that bear directly on the question, including grey literature (preprints, dissertations) where it turns up. If OpenAlex refuses requests because the shared daily budget is spent, say so: the user needs a free key, in `OPENALEX_API_KEY` or in the file `~/.openalex-tok`.
3. **Extract, one paper per reader.** If your harness has subagents and the `paper-extractor` agent is available, hand each paper to one. Extraction does not need the strongest model. It needs a reader with one paper open and no account of the question to defend, which is what the extractor is: it runs on a mid-tier model with a fresh context. Do not move it to the smallest tier, which in testing lost the claim under test and had half its batches rejected. The framed hypotheses and conditions are already in the subgraph, so every extractor starts from the same ids; if reading has shown you another hypothesis or distinction the question turns on, add it with `scibraid frame` before you delegate. Give each extractor the slug, the question, the paper id and one line on why the paper was retrieved. Start the most central paper alone so that its conditions are in the subgraph when the rest begin, then run the others a few at a time. `scibraid add` is safe to call concurrently. If you have no subagents, extract each paper yourself as described under Extracting a paper, one at a time.
4. **Synthesise.** This step is yours and is not delegated: it needs the whole question and every paper in view, which no extractor had. Read `scibraid show <slug>` in full. Extractors working at once cannot see each other's ids, so `scibraid duplicates <slug>` lists conditions and hypotheses that may be one thing under two ids. Most pairs it lists are contrasts (zero-shot, few-shot), not duplicates; where a pair really is one thing, `scibraid merge <slug> <keep> <drop>`. Then add what no single-paper reader could: `contradicts` between observations from different papers, `competes_with` between hypotheses, `fails_to_replicate`, and the model-asserted `supports` or `contradicts` from an observation to a hypothesis its authors never mention. Check that each paper's evidence is connected to the hypotheses in contention and not only to a restatement of its own abstract, and go back to the source yourself for any result the question turns on.
5. **Check.** `scibraid lint <slug>` and act on what it finds. Also run `scibraid repair list`: if a repair names this subgraph or one of its nodes, a later check found a fault in it. Fix it with another `add`, then `scibraid repair resolve <lead-id> <n> --note "what changed"` and pool again. A finding you cannot fix from the sources is fine; say so in your summary.
6. **Report.** Summarise what the graph shows: where the evidence sits, where it conflicts and under which conditions, what is thinly evidenced, and what you could not verify. `scibraid show <slug> --format mermaid` gives a diagram.
7. **Close the follow-up.** If this subgraph answers a lead's follow-up, say in your report what it means for the lead: the literature settles the claim (the lead becomes `known` or `refuted`), or it does not, in which case the claim is an open research question and the lead becomes `open`, with the experiment that would settle it. Updating the lead is the `pursue-leads` skill's job; give it what it needs.
8. **Pool.** `scibraid pool <slug>`. With `SCIBRAID_POOL_URL` unset the pool is a local file and you can just do it. If it is set, pooling publishes the subgraph to a shared server: ask the user first.

## Framing

A question is posed with more than its one line. Whoever asks it often has hypotheses in mind, a distinction they need kept (in their design the agents were told something, which is not the same as its being true), and literatures they know are relevant but that do not use the question's words. Anything the user writes after the question is framing: take it as given. When they give none, frame the question yourself. Either way it is recorded on the subgraph, because what a review was told to look for shapes what it found, and someone reading the pooled subgraph later is entitled to know.

`--hypothesis "text"` adds a hypothesis to be tested. Use the user's wording for theirs. A framed hypothesis is a claim under test and has no standing until evidence is linked to it; it is not a finding, and finding nothing for it is a result. Always add the rivals the user did not give, from the literature as you come to know it: a review framed only by its author's favoured hypotheses will confirm them.

`--condition "text"` names a distinction that every extractor must record whenever a paper allows it, such as whether a manipulation was real or only announced. Seed the two sides as two conditions. This is what stops parallel extractors splitting one distinction across several ids or folding it into one, and it is what lets the pool later say whether anyone ran the combination the user cares about. An id can be fixed by writing `"c:my-id=Label"`.

`--brief "text"` says how to steer the search and the extraction: literatures to cover beyond the obvious one, terms those literatures use, and what to look for in methods sections. Extractors see it when they run `scibraid show`.

`scibraid frame <slug> ...` takes the same options for a subgraph that already exists, which is how framing is added part-way through. `scibraid lint` reports a framed hypothesis that ended with no evidence, and a framed condition that no experiment was recorded under. Treat the second with suspicion before you report it as a gap in the literature: read the methods sections of the most relevant papers yourself, since a condition the paper used but nobody recorded looks the same as one nobody ran.

## Extracting a paper

Read the paper (`scibraid paper show <id>`, and the full text where you can get it: see Beyond the abstract). Run `scibraid show <slug>` immediately before writing, and reuse the ids that are there instead of minting duplicates. Write a JSON batch and apply it: `scibraid add <slug> batch.json --model <your model id>`. Other sessions and other extractors may be working in the same directory, so write batch files to your own scratch directory or give them names no one else will use. A batch is all-or-nothing; fix what the tool reports and re-run.

## Node types

| type | what it is | id prefix |
|---|---|---|
| `hypothesis` | A claim about the world that evidence could bear on. | `h:` |
| `experiment` | A specific study, arm or analysis that was actually run. One per study in a multi-study paper. | `e:` |
| `condition` | A circumstance an experiment was run under: population, species, dose, task, temperature, measure, preregistration, sample size band. | `c:` |
| `observation` | What was seen, stated without explanation. Needs an `outcome`: `positive`, `negative`, `null`, `inconclusive`, `mixed` (relative to what the experiment was looking for). | `o:` |
| `interpretation` | An explanation someone offered for an observation. | `i:` |

Ids are lowercase kebab slugs, e.g. `c:sequential-task-paradigm`. Make condition and hypothesis ids generic and reusable ("hypoxia", not "hypoxia in Smith 2019"), since they are what different papers, and later different subgraphs, share. Experiments and observations are particular, so name them after the study (`e:hagger-2016-multilab`).

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

Every edge needs `confidence` (0-1), `asserted_by`, and provenance from exactly one paper. The same relation evidenced by a second paper is a second edge: that is how evidence accumulates, and how a later source weakens an earlier one.

## Rules that matter

**Separate what was seen from what was said about it.** Authors slide between result and explanation within a sentence. "Depletion reduced persistence, consistent with a limited resource" is an observation (reduced persistence) plus an interpretation (limited resource). Split them. The observation `supports` the hypothesis; the interpretation `explains` the observation.

**`asserted_by` is about who drew the link.** `author` if the paper itself states the relationship; `model` if you inferred it. A paper states its own design and results, so `performed_under`, `yields` and `tests` are nearly always `author`: "we evaluate all models with an 8-shot prompt" is the authors saying the experiment ran under 8-shot prompting. A null result whose authors never mention the hypothesis it undermines is `contradicts`, `asserted_by: model`. Model-asserted edges are welcome, they are often the interesting ones, but they must be labelled.

**Confidence is how sure you are the relationship holds as stated, given this passage** - not how good the paper is, and not how strong the effect was. Rough guide: 0.9+ the paper says it in so many words; 0.7-0.85 clear from the passage with little inference; 0.4-0.65 a reasonable reading that another reader might dispute; below 0.4 probably not worth recording. `scibraid add` rejects a model-asserted edge above 0.85.

**Passages are verbatim.** Copy the quote exactly from the abstract or attached text; `...` may elide words within a quote. `scibraid add` rejects a passage that is not in the text it holds, or that starts or ends mid-word (the mark of a quote copied from truncated output rather than read; go back to the source for the whole sentence). When that happens, re-read and fix the quote - never paraphrase into quotation marks, and never weaken the check by quoting two words. Quote enough that a reader sees the relationship in the passage.

**Failures are first-class.** Null results, failed replications, abandoned approaches and "did not reach significance" are constraints on hypothesis space, and the literature under-reports them. Record them with the same care as positive results, with their conditions: a failure is only informative alongside the circumstances it failed under.

**Conditions are the alignment surface.** Record every condition the source gives you, even ones that seem irrelevant to this question. Put quantities in `attrs` (`{"n": 2141, "labs": 23, "dose_mg_kg": 5}`) and keep the label human-readable.

**Do not smooth over disagreement.** If two papers conflict, record both and a `contradicts` edge between the observations. Do not pick a winner.

## Beyond the abstract

Abstracts omit conditions and nulls. `scibraid fetch <id> ...` (or `--subgraph <slug>` for every paper a subgraph cites) finds an open copy and attaches its text: arXiv's HTML, Europe PMC's XML, or a PDF, in that order. Section headings appear as `## ` lines and inline mathematics as the LaTeX the source carried, so quote a formula as the text shows it. Then cite with `"location": "methods"` (or `results`, `discussion`). Text that is already attached is left alone unless you pass `--force`, because recorded passages were checked against it. Where `fetch` finds nothing, and you can get the text another way, save it as plain text and attach it with `scibraid paper text <id> fulltext.txt`. A passage from text that is not attached is accepted with a warning and stays `verified: null`; keep those few.

For a source OpenAlex does not have (try `scibraid paper get` first), write a paper JSON (`id`, `title`, and what you know of `doi`, `year`, `authors`, `venue`, `url`, `abstract`, `source_tier`: `published` | `grey` | `process`) and `scibraid paper add paper.json`.

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

Nodes may carry provenance too (useful for experiments and observations); edges must. Re-adding an existing node id merges provenance; re-adding an edge from the same paper replaces it, so correcting an extraction is just another `add`.
