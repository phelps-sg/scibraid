---
name: align-subgraphs
description: Align pooled evidence subgraphs and read off what the pooled structure shows. Judges whether nodes from independently built subgraphs are the same thing, records the verdicts as links, then reports candidate observations (conditions reached from different questions, failures sharing a condition, results that may bear on another question's hypothesis). Use after two or more subgraphs have been pooled, or when asked to "align", "find links between subgraphs", or "what does the pool show".
---

# Align subgraphs

Subgraphs in the pool were built by different people asking different questions. They name the same things differently and different things the same. Alignment decides which is which, without merging anything: a verdict is a link between two nodes, with its own confidence and rationale, that anyone can inspect and revise.

The work is split by cost. `scibraid candidates` cheaply proposes pairs, ranked by how plausible the match is and by how much pooled structure it would join if true. You are the expensive stage: judge the pairs it ranks highest, and stop when the budget is spent. A pair it does not propose is simply not yet judged, and if you notice one while reading, judge it anyway: `align add` accepts any pair.

With the `embeddings` extra installed, plausibility is the mean of word overlap and embedding similarity. Embeddings find matches that share no words ("models well below 100B" and "open models spanning 500M to 70B") and also propose pairs that merely sound alike ("GPT-5 family" and "T5", "sparse autoencoders" and "sparse transformers"). Expect about a third of what they add to be real.

## Workflow

1. `scibraid pool --list` to see what is pooled. Fewer than two subgraphs: nothing to do. `scibraid repair list` shows any verdicts that a later check found wrong (`kind: alignment`, target `a ~ b`): re-judge those first, replace them with `align add`, and `scibraid repair resolve <lead-id> <n> --note "..."`.
2. `scibraid candidates [--budget 40] [--type condition]` returns JSON pairs. Each has both nodes' labels, descriptions, attrs and `context` (how each node is used in its own subgraph), and the signals behind the score. Pairs already judged are not proposed again.
3. Judge each pair, write a JSON list, and `scibraid align add verdicts.json`. Run `candidates` again until it proposes nothing you have not judged.
4. Hypotheses are handled differently. `scibraid hypotheses` prints, for each pair of subgraphs, both hypothesis lists in full, with the verdicts already recorded between them. Pairs of subgraphs that aligned conditions already bridge come first. Read both lists and nominate the pairs yourself: whether one hypothesis bears on another is not a matter of similarity, and no cheap signal finds it. Record `same`, `broader`, `narrower` and `related` pairs, and `different` only for tempting look-alikes. Do this after the conditions, since knowing what the two subgraphs share tells you where their hypotheses might meet.
5. `scibraid observe` and report what it shows (below).
6. If an observation looks important and rests on a link you were unsure of, go back to the sources (`scibraid paper show <id>`, the subgraph's passages via `scibraid show <slug> --format json`) and revise the verdict: `align add` with the same pair replaces it.

## Verdicts

```json
[{"a": "cot-scale-threshold/c:palm-models", "b": "llm-emergent-abilities/c:model-family-palm",
  "verdict": "same", "confidence": 0.95,
  "rationale": "Both are 'experiments run on PaLM models'; sizes listed on one side only."}]
```

| verdict | meaning |
|---|---|
| `same` | Interchangeable for the purpose the nodes serve. An experiment attached to one could equally be attached to the other. |
| `broader` / `narrower` | `a` is broader / narrower than `b`: every case of one is a case of the other, not the reverse. |
| `related` | Genuinely connected (overlapping, one a special case under conditions, cause and effect) but neither contains the other. The only verdict allowed across node types. |
| `different` | Not the same thing, including opposites and look-alikes. Record it: it stops the pair being proposed again and documents a trap. |

`confidence` is how sure you are of the verdict, not how similar the labels are. When `observe` reads the pool, `same` at 0.7 or above joins two nodes, and `narrower` or `broader` at 0.7 or above lets an experiment under the narrower condition count as under the broader one (never the reverse). `related` joins nothing. So a `same` or `narrower` you would not stake an inference on should be `related`, or carry a lower confidence. `rationale` must say what decided it, in a sentence a sceptical reader could check.

## How to judge

**Judge by use, not by name.** Read the `context`. Two conditions called "few-shot prompting" are different if one subgraph means "with chain-of-thought exemplars" and the other means "with answer-only exemplars": the contrast between those is exactly what one of the questions is about. Two conditions with different names are the same if experiments could be swapped between them without loss.

**Watch for opposites.** Lexical matching ranks "instruction-tuned" next to "without instruction tuning". Shared vocabulary is evidence of a shared topic, not a shared referent.

**Prefer the asymmetric verdict when there is one.** "BIG-Bench Hard" is narrower than "BIG-Bench", not the same: a claim about BBH tasks does not transfer to all of BIG-Bench, though the reverse may.

**Same paper, same study is usually `same`** for experiments, even when each subgraph emphasised a different result from it. Observations from the same experiment are `same` only if they report the same finding.

**Hypotheses rarely match exactly.** Ask: would evidence for one count as evidence for the other? If yes both ways, `same`. If one way, `broader`/`narrower` (a claim about all abilities is broader than the same claim about one ability). If they bear on each other without entailment, `related`, and say how in the rationale, since that sentence is often the most valuable thing alignment produces.

**Derived hypotheses are already linked.** A subgraph started from a lead carries that lead's claim as a hypothesis, and pooling links it as `related` to the hypotheses the lead rested on, at the lead's confidence. Leave those links alone unless the follow-up's evidence changes the picture, and judge the derived hypothesis against everything else as you would any other.

**Do not align to make the pool more interesting.** A false `same` fabricates structure that every later observation inherits.

## Reporting what the pool shows

`scibraid observe` reads the aligned pool and returns candidate observations:

- **bridging conditions**: one condition reached independently from different questions, with the experiments and hypotheses on each side;
- **shared-condition failures**: negative, null or inconclusive results from different papers under one condition;
- **cross-bearing**: a result from one question that sits under the conditions another question's hypothesis is tested under, scored so that rare conditions count for more than ubiquitous ones;
- **contradictions by regime**: conflicting results with the conditions unique to each side;
- **linked hypotheses** and their pooled evidence; **thinly evidenced** ones.

These are leads, not findings. For each one you report, say what it rests on (which alignment links, at what confidence) and what would confirm or refute it. The useful ones suggest a new question: hand it back to the `evidence-subgraph` skill.
