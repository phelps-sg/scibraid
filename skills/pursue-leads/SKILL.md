---
name: pursue-leads
description: Turn what the aligned pool suggests into checked, recorded leads. Takes the candidate observations from `scibraid observe` (shared-condition failures, cross-bearing results, contradictions by regime, experiments nobody ran), checks each against the sources and the wider literature, and records it with what it rests on, what would settle it, and the next question to ask. Use after subgraphs have been aligned, or when asked "what does the pool tell us", "find insights", "what should we look at next", or to "pursue", "check" or "follow up" a lead.
---

# Pursue leads

`scibraid observe` reads structure off the aligned pool. What it returns are coincidences of structure: two failures under one condition, a result sitting under another question's conditions, a hypothesis never tested under a condition that matters for its neighbour. Some of these are insights. Most are artefacts of how the graphs were built, or things the field already knows. Your job is to find out which, and to record the answer so that it is not lost.

A lead is recorded as carefully as a link. It names the nodes and alignment verdicts it rests on, the checks that were made, what would confirm and what would refute it, and the next question. A lead that turns out to be already known, or wrong, is still worth recording: it stops the next person chasing it.

## Workflow

1. `scibraid observe --new --format json` for candidates that no recorded lead covers yet, and `scibraid lead list` for what has been pursued. Without `--new` each candidate carries the `leads` that cover it, with their status. Return to a covered candidate only if new subgraphs or verdicts bear on it.
2. Triage. Pick the few worth the effort (see below). Five well-checked leads beat twenty unchecked ones.
3. For each, make the checks below, going back to the sources.
4. Write the leads as a JSON list (in your own scratch directory) and `scibraid lead add leads.json --model <your model id>`. The tool records who did the work: the person, the harness and the session come from the environment, but it cannot see which model you are, so pass `--model <your model id>` on `lead add`; `scibraid agenda` then shows whether each open question was checked by a different reader from the one who built its evidence. Re-adding an id replaces it, which is how a lead's status changes. To change an existing lead, start from `scibraid lead list --format json` and keep its `updated` field as you found it: the tool uses it to tell whether someone else changed the lead after you read it, and refuses a stale copy.
5. Report: what holds, what was already known, what fell over and why. `scibraid followups` lists every lead's follow-up question and how far the work on it has got (`pending`, `in_progress`, `reviewed`). A pending one is taken up with `scibraid new <slug> --lead <lead-id>` and the `evidence-subgraph` skill, which is how the pool grows where it matters. `scibraid agenda` lists the open research questions, which are the point of the whole exercise.

## Triage

Prefer a candidate when the condition it turns on is specific (a high `specificity`; a condition nearly every experiment shares explains nothing), when it joins subgraphs built for different questions, when the results come from different papers and groups, and when it would change what someone does next. Under `absent_experiments`, prefer pairs of hypotheses whose `rationale` already says why one should bear on the other.

Be suspicious of a candidate that merely restates an alignment (two hypotheses were judged related, and their experiments share model families: nothing new), and of anything resting on a `same` verdict you would now doubt. Fix the verdict first, with the `align-subgraphs` skill.

## The checks

Make each check that applies, and record it as a `question` and a `finding`, with the paper ids or URLs you consulted. A check that finds nothing wrong is still a finding: say what you looked at.

**Is the premise true in the sources?** Read the passages behind the nodes (`scibraid show <slug> --format json`, `scibraid paper show <id>`, and the full text if it was attached). Is the shared condition really the same in both papers: the same model, dose, population, task? Do the outcomes mean what the lead needs? An observation's `outcome` is relative to what its own experiment was looking for, so two "negative" results can point in opposite directions. Read them; do not count them.

**Is the gap in the literature or only in the graph?** An experiment nobody ran and an experiment nobody recorded look identical in the pool. Before claiming a hypothesis was never tested under a condition, check the source papers for it (metrics and prompting details are often used but not recorded as conditions), and search for work that did run it: `scibraid search`, and the web if you have it.

**Is there a duller explanation?** A confound that travels with the shared condition; a second condition that differs between the results and explains them better; a selection effect in which papers were found. If the dull explanation is as good, the lead is `refuted`, or survives only at low confidence with the confound named.

**Does the field already know?** Search for the connection stated outright. Check whether either source paper cites the other or makes the link itself in its discussion. If it does, the lead is `known`: record where in `known_in`. That is a useful result. It shows the method recovering real structure, and it tells a newcomer something that took the field years to say.

**Has anyone asked the question?** This is a different check from whether anyone has answered it. A claim the literature does not settle may still be one the field already names as unresolved: look for it stated as a hypothesis, a conjecture, "unverified", "remains unclear" or listed under future work, in the source papers' discussions and in a search. Record where in `posed_in`. If you looked and found nowhere, record an empty list. Leaving `posed_in` unset means nobody looked, and the tool will not accept `open` that way. An open question that is already posed is still worth having, since the pool adds the evidence around it and the experiment that would settle it. But it is not a question the pool discovered, and the agenda keeps the two apart.

## Recording

```json
[{"id": "instruction-tuning-gates-both",
  "claim": "A single sentence stating what may be true of the world, not what the graph looks like.",
  "kind": "shared_failure",
  "status": "holds",
  "confidence": 0.5,
  "nodes": ["cot-scale-threshold/o:...", "llm-emergent-abilities/o:...", "cot-scale-threshold/c:...", "llm-emergent-abilities/c:..."],
  "alignments": [["cot-scale-threshold/c:...", "llm-emergent-abilities/c:..."]],
  "checks": [{"question": "Is the base model the same in both papers?", "finding": "Yes: both use ...", "sources": ["W4402671818", "W4385571045"]}],
  "would_confirm": "The experiment or observation that would settle it in favour.",
  "would_refute": "The result that would sink it.",
  "known_in": [],
  "posed_in": [],
  "follow_up": "A research question, phrased so it can be handed to evidence-subgraph as it stands."}]
```

If a check shows that a subgraph or a verdict is wrong, add a repair to the lead instead of fixing it from here: `"repairs": [{"kind": "extraction", "target": "<slug>" or "<slug>/<node id>", "problem": "..."}]`, or `"kind": "alignment"` with `"target": "<a> ~ <b>"`. The target must exist in the pool. `scibraid repair list` shows open repairs to whoever owns the subgraph or the verdicts.

`kind` is the `observe` section the lead came from: `bridge`, `shared_failure`, `cross_bearing`, `regime`, `untested`, or `other` for something you noticed yourself. `status` is `candidate` (not checked), `holds`, `open`, `known`, or `refuted`. The tool rejects a lead that is judged without checks, `known` without saying where, or `holds` or `open` without saying what would confirm and refute it.

`holds` and `open` differ in how much looking stands behind them. `holds` is provisional: the premise is verified, no duller explanation was found, and a brief search did not turn the connection up. `open` means an open research question: the literature has been reviewed and does not settle the claim, so only new empirical work can. Reserve it for that. A lead with a follow-up question can become `open` only after that follow-up has been reviewed, which means a subgraph started from the lead has been built and pooled, and you have read what it found. If that review settles the claim, the lead becomes `known` or `refuted` instead. If no question the literature could answer applies, leave `follow_up` empty and the lead can go straight to `open`. For an `open` lead, `would_confirm` is the experiment someone should run: write it so a researcher in the field could act on it, and `posed_in` must say whether the literature already asks the question. `open` is where this pipeline stops. Another review will not help, and the question goes to a person.

`confidence` is how likely the claim is to be true of the world, given everything you checked. It cannot exceed the confidence of the weakest alignment verdict the lead rests on, and the tool enforces that. It should usually be well below it: an alignment being right is necessary, not sufficient. Two papers and one shared condition is thin evidence, so most leads that hold belong between 0.3 and 0.6.

Write the `claim` about the world ("instruction tuning, not parameter count, gates chain-of-thought gains"), not about the graph ("two observations share a condition"). Write `follow_up` as a question a literature review could answer; if only a new experiment could answer it, say so in `would_confirm` and make the follow-up the nearest question the literature can address.

## Checking your own work

If you built the subgraphs a lead rests on, your check is a re-reading, not a second reading: you will tend to find what you found before. Do the checks anyway, say so in your report, and prefer leads whose evidence someone else built. `scibraid agenda` reports this as the relation between the checker and the builders of the evidence (`same reader`, `same model`, `different model`).

## What not to do

Do not promote a coincidence because it is interesting. Do not raise a lead's confidence because several candidates point the same way when they rest on the same two papers. Do not delete a lead that failed: set it to `refuted` and keep the check that sank it. Do not edit subgraphs or verdicts from here to make a lead work; if a check shows an extraction or an alignment was wrong, record a repair.
