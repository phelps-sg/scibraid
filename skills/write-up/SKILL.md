---
name: write-up
description: Write a synthesis paper, in LaTeX that arXiv accepts, from pooled evidence subgraphs and the leads drawn from them. Takes a steer or focus area that says what the paper is about, and writes in the voice of a configurable exemplar paper. Every claim rests on the pool, every reference comes from cached metadata and never from memory, and every quotation is checked against its source. Use when asked to "write up", "write a paper", "draft a review" or "synthesise" what the pool shows on a topic.
---

# Write-up

A pool of evidence subgraphs is a structured literature review that nobody can read. This skill turns the part of it that bears on one topic into a paper: an argument about what the evidence shows, where it conflicts and under which conditions, and what remains open, written so that a researcher in the field would read it and could check it.

The paper is a synthesis, not an inventory. It is organised by the claims in contention and the conditions that separate results, not paper by paper and not subgraph by subgraph. It asserts only what the pool supports, and it says how the review was made, including what was read by which model and what could not be read at all.

## What the user gives

- **A steer.** Whatever follows the command is the focus: the topic, the angle, the audience, the claim they suspect, a section they want. The steer chooses what the paper is about. It does not choose which evidence counts: results that cut against the steer go in with the same prominence as those that favour it, and a paper that finds the steer's suspicion unsupported is a good outcome. If there is no steer, ask for one; "write up everything" produces an inventory.
- **Optionally** which pooled subgraphs to rest on, which voice exemplar to use, where to put the draft, and the authors. Never invent authors, affiliations, acknowledgements or funding. Leave the template's placeholders in place and say so.
- **Where it goes.** The directory the user names; failing that, wherever the user's standing instructions say drafts live; failing that, `writeups/<short-slug>/` under the working directory.

## Workflow

1. **Scope.** `scibraid pool --list`, `scibraid lead list` and `scibraid agenda`. Choose the pooled subgraphs the steer bears on, including any whose evidence cuts against it. A subgraph that is local but not pooled cannot be used: pool it first, or leave it out and say so. If the choice is not obvious, tell the user which you chose and why before going on.
2. **Start the draft.** `scibraid draft start <dir> <slug> [<slug> ...] --focus "<the steer>" [--voice <name>]` writes the dossier, the references, a skeleton and a record. `dossier.md` is the overview: how the review was made, every hypothesis with its balance of evidence, how hypotheses in different subgraphs bear on one another, the leads with their checks, and the citation keys. `evidence-<slug>.md`, one per subgraph, holds the evidence itself under a heading for each hypothesis: the observations for and against it, the conditions each was obtained under, the passage it rests on and its citation key, and the results recorded as contradicting each other. A pool's evidence on a topic is more than can be held at once, so read the overview in full and the evidence a hypothesis at a time, before writing about that hypothesis. `refs.bib` holds a reference for every paper, generated from cached metadata. `main.tex` is a skeleton arXiv accepts. Running the command again refreshes the dossier and the references and leaves `main.tex` alone.
3. **Take the voice.** The command reports the voice exemplar and where its text is (`scibraid voice show`). Read it and write `voice.md` in the draft directory, as described under Voice. If no exemplar is set, say so, and write in the plain register described under Prose.
4. **Spine.** Write `spine.md`: the thesis in one sentence; the argument as a short numbered backbone, each step naming the dossier evidence it rests on; each section's job in one plain line; and what the paper will say is not known. Show the spine to the user and wait for their answer, unless they asked you to go straight through. The spine is the cheap place to find out that the steer was misunderstood, and the paper is rebuilt from it if the draft goes wrong.
5. **Draft.** Write `main.tex` from the spine, the dossier and the voice sheet. If you are the top-level session and the `synthesis-writer` agent is available, hand it steps 3 to 5 in two calls (spine, then draft once the user has agreed the spine), giving it the directory, the steer and what the user said about the spine: it runs on the strongest model whichever model you are. If you are that agent, or have no subagents, do it yourself.
6. **Check and edit.** `scibraid draft check <dir>` must pass: every citation resolves, every quotation of eight words or more is verbatim in a source its paragraph cites, the abstract is within arXiv's 1,920 characters, paths are relative, and the paper compiles. It also lists prose tells with line numbers. Treat that list as the start of the edit described under Prose, not the whole of it. Then read the PDF.
7. **Report.** Where the draft is, what it argues in three sentences, what it leaves open, which claims you were least sure how to state, what the check still reports, and what the user must supply (authors, anything from their own unpublished work, figures). Do not submit anything anywhere.

## What the paper may say

**Only what the dossier holds.** A claim about a paper's result needs that result in the dossier's evidence files, cited with its key; the overview's counts are not evidence of anything and are never quoted as a balance. Take numbers from the quoted passages, not from memory of the paper; if the passage does not give the number, read the attached text (`scibraid paper show <id>` says whether there is one; `scibraid fetch <id>` gets one) or leave the number out.

**References come from metadata, never from memory.** To cite something the pool does not hold (the classic statement of a theory, a method), run `scibraid draft cite <dir> <DOI or arXiv id>`, which fetches the record, adds it to `refs.bib` and prints the key. If it cannot be found, do not cite it. A reference written by hand is how invented citations get into papers, and the check rejects any key that is not in `refs.bib`. A paper cited this way has not been read by anyone in the pipeline: cite it for what it is known for, not for a specific result.

**The pool's words have narrow meanings.** A lead that is `open`, or a question "posed nowhere", means a brief search found nothing, not that nothing exists: write "we found no study that" and not "no study has". A condition with no experiment under it means none was recorded. An observation's outcome is relative to what its own experiment looked for, so do not add up positives and negatives across papers. Counts of supporting and contradicting observations describe the review, not the world; never present them as a vote.

**Who inferred what.** The dossier marks each link as drawn by the paper's authors or inferred by the reader. An inferred link is the review's reading and the paper must own it as such ("we read this result as bearing on"), not attribute it to the cited authors.

**Independence.** Say which model read what, and whether anything had a second reader of a different kind. Two subgraphs built by the same reader agreeing is one reading. Two papers from one group are not two confirmations.

**What was not read.** Closed papers read from the abstract only, papers that search may have missed, and the retractions on record belong in the paper.

**Nothing from outside the pool about the user's own work.** If the steer mentions results the user has and the pool does not, leave a marked gap for them to fill. Do not describe, guess at or pre-empt unpublished results.

## Shape

There is no fixed outline; the spine decides. Most synthesis papers need the question and why it is unresolved; the method of the review; the evidence, organised by claim and by the conditions that separate results; the conflicts and what may explain them; what is open, with the experiment each question needs; and the limitations. The method section is not optional and not an apology: it states the questions asked, how papers were found, that extraction was done by language models with every quotation checked against the source, which models, what a human checked, and the counts from the dossier. arXiv requires that substantial use of generative tools be disclosed and does not accept them as authors; the method section is that disclosure.

Tables earn their place where the reader needs to compare studies on the same conditions: one row per experiment, columns for the conditions that matter, `booktabs`, no vertical rules. Do not draw a figure from numbers you do not have. A figure the user would need to make is a line in your report, not a placeholder in the paper.

## Voice

The exemplar is a paper whose voice the user wants: usually their own. Read all of it, then write `voice.md`, a page at most, recording what you observe and would otherwise drift from: person and tense (does it say "we", and for what); how long its sentences and paragraphs run; where it puts a claim and where its qualifications; how it introduces other people's work (by author, by finding, by school); how much it explains to a reader from the neighbouring field; how it handles formalism and numbers; how it opens and closes sections; what it never does. Quote three or four short sentences that are characteristic.

Take the voice and leave the words. No sentence, phrase or structure of argument is lifted from the exemplar; the quotations in `voice.md` are for your ear and stay there. The exemplar's subject and its field's conventions are not the new paper's: where they differ (citation style, what counts as evidence, what needs defining) the new paper's field wins. If the exemplar's habits conflict with the rules under Prose, the exemplar wins on register and rhythm, and the rules win on honesty: a voice is never a reason to overstate.

## Prose

Machine-sounding prose is mostly accretion. Each pass adds a clause, a caveat, a cross-reference, a sentence announcing what the next sentence will say, until every claim carries four qualifications and nothing is asserted. Editing such prose in place adds more. That is why the paper is written from a spine, and why a section that has gone wrong is rewritten from its line in the spine and not polished.

There are two ways to fail. One is the default: ornate, hedged, signposted, every paragraph opening with a frame and closing with a moral. The other is the over-correction: short punchy fragments, rhetorical questions, one-line paragraphs, the cadence of a blog. The target is the calm middle: plain, measured sentences that make claims, in the register of the exemplar.

- One idea to a sentence. Split any sentence carrying two qualifying clauses.
- Claim first. Genuine uncertainty is collected where it belongs, in the limitations or beside the one claim it qualifies, and is not sprinkled through every sentence. Cutting a hedge never means dropping a real qualification: move it.
- No sentence that only announces. "It is worth noting that", "as we will see", "this section describes", "the key insight is": delete the frame and keep the claim. Do not narrate the paper's own choices or what it declined to do.
- Contrasts, triads and closing aphorisms only when the content has that shape. "Not X but Y" is for a real contrast.
- Plain words. No puffery (robust, crucial, landscape, leverage, shed light, a growing body of), no empty intensifiers.
- Most em-dashes are full stops. Bold is for a term being defined, never for emphasis. No rhetorical questions.
- Paragraphs carry sequence without Moreover and Furthermore. Ordinals are fine on a real list. Lead a list with what its items amount to, not with a bare count. A short list of paragraph-sized items is paragraphs, not subsections.
- A heading names its topic in a few words and does not make the claim.
- End on a claim, not a recap.
- Precision over vividness: the exact true sentence, not the punchier one that overstates.

Authority comes from stating what the evidence shows and defending it, not from confidence. Concede the real objection in its strongest form, mark conjecture as conjecture, and let the limitations section be specific.

## Format

`main.tex` stays a single file in the `article` class with the packages the skeleton loads, `\pdfoutput=1` in its first lines, `natbib` with `\citet` and `\citep`, and `refs.bib` beside it. arXiv compiles the source, so everything it needs must be in the directory with relative paths, including `main.bbl`, which the check produces. Write LaTeX by hand: escape `%`, `&` and `_`; use `` ` ` `` and `''` for quotation marks, which is also how the check finds quotations; put each paragraph on one line.
