"""What a write-up needs from the pool, and the checks a draft has to pass.

The writing is the agent's. This module gives it a dossier (the evidence, stated once, with a
citation key for every paper), a LaTeX skeleton that arXiv will accept, and a check that the draft
cites only what the dossier holds, quotes its sources verbatim, and compiles.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from collections import defaultdict
from pathlib import Path

from . import bibtex, identity, store
from .align import _Lexical
from .embed import Embedder, calibrated
from .embed import cosine as cosine_of
from .models import Alignment, Lead, NodeType, Paper, Relation, Subgraph, _now, surname

ABSTRACT_LIMIT = 1920  # characters, arXiv's limit

TEMPLATE = r"""\pdfoutput=1
\documentclass[11pt]{article}
\usepackage[T1]{fontenc}
\usepackage[utf8]{inputenc}
\usepackage{lmodern}
\usepackage[margin=1in]{geometry}
\usepackage{microtype}
\usepackage{amsmath}
\usepackage{graphicx}
\usepackage{booktabs}
\usepackage[round]{natbib}
\usepackage[hidelinks,colorlinks=false]{hyperref}
\usepackage{url}

\title{TITLE}
\author{AUTHOR NAME\\ \small Affiliation\\ \small \texttt{email}}
\date{}

\begin{document}
\maketitle

\begin{abstract}
ABSTRACT
\end{abstract}

\section{Introduction}

% Sections follow the spine. Cite with \citet{key} and \citep{key}; every key is in refs.bib, and
% `scibraid draft cite <dir> <DOI or arXiv id>` adds one. Do not write a reference by hand.

\section{Method}

% How the review was made: the questions asked, how papers were found and read, what was checked
% and by whom. dossier.md has the counts under "How this review was made".

\section{Limitations}

\bibliographystyle{plainnat}
\bibliography{refs}

\end{document}
"""

# Phrases that frame, soften or decorate where a sentence should assert. Advisory: a draft may keep
# one where it is earning its place, but a count in the dozens means the prose has not been edited.
TELLS = {
    "signposting": [
        "it is worth noting", "it is worth emphasi", "it is worth stating", "it is important to note", "it should be noted",
        "importantly,", "notably,", "crucially,", "interestingly,", "the key insight", "a central result is", "what is crucial",
        "as we will see", "as noted above", "as mentioned above", "in what follows", "this section", "in this section",
        "the remainder of this paper", "the rest of this paper", "we now turn to", "let us", "make no mistake", "the truth is",
    ],
    "hedging": [
        "that said,", "at the end of the day", "in many ways", "to some extent", "in a sense", "arguably", "somewhat", "relatively speaking",
        "it could be argued", "it may be the case that", "perhaps", "may potentially", "might possibly",
    ],
    "puffery": [
        "delve", "tapestry", "testament", "underscore", "boasts", "leverage", "landscape", "realm", "robust", "crucial", "vital",
        "seamless", "vibrant", "cutting-edge", "game-chang", "unlock", "harness", "foster", "myriad", "plethora", "pivotal", "nuanced",
        "truly", "incredibly", "remarkably", "fundamentally", "groundbreaking", "paradigm", "holistic", "shed light", "sheds light",
        "paves the way", "a growing body of",
    ],
    "filler connectives": ["moreover,", "furthermore,", "additionally,", "in addition,"],
    "recap": ["in conclusion,", "to sum up", "in summary,", "we have seen that", "to summarise", "to summarize", "overall,"],
    "negative parallelism": ["not just ", "not only ", "it is not about", "isn't about", "rather than merely"],
}


# ---- voice exemplars ------------------------------------------------------------

def voice_dir() -> Path:
    path = store.home() / "voice"
    path.mkdir(parents=True, exist_ok=True)
    return path


def voices() -> list[dict]:
    return [json.loads(p.read_text()) for p in sorted(voice_dir().glob("*.json"))]


def save_voice(name: str, text: str, meta: dict, default: bool) -> dict:
    existing = voices()
    meta = {"name": name, "chars": len(text), "added": _now(), **meta, "default": default or not existing}
    if meta["default"]:
        for other in existing:
            if other["name"] != name and other.get("default"):
                other["default"] = False
                (voice_dir() / f"{other['name']}.json").write_text(json.dumps(other, indent=2))
    (voice_dir() / f"{name}.txt").write_text(text)
    (voice_dir() / f"{name}.json").write_text(json.dumps(meta, indent=2))
    return meta


def voice(name: str | None = None) -> tuple[dict, Path] | None:
    for meta in voices():
        if meta["name"] == name or (name is None and meta.get("default")):
            return meta, voice_dir() / f"{meta['name']}.txt"
    return None


# ---- citation keys --------------------------------------------------------------

def assign_keys(keys: dict[str, str], papers: list[Paper]) -> dict[str, str]:
    """Keys for papers that have none, leaving existing keys alone so a draft's citations stay valid."""
    keys = dict(keys)
    for paper in sorted(papers, key=lambda p: (p.year or 0, p.title)):
        if paper.id in keys:
            continue
        base, taken, n = bibtex.cite_key(paper), set(keys.values()), 0
        key = base
        while key in taken:
            key = base + chr(ord("a") + n)
            n += 1
        keys[paper.id] = key
    return keys


def bib(keys: dict[str, str], papers: dict[str, Paper]) -> str:
    return "\n\n".join(bibtex.entry(papers[pid], key) for pid, key in keys.items() if pid in papers) + "\n"


# ---- the dossier ----------------------------------------------------------------

def _relevance(focus: str, texts: dict[str, str], embedder: Embedder | None) -> dict[str, float]:
    if not focus or not texts:
        return {}
    lexical = _Lexical({"?": focus, **texts}, {"?": focus, **texts})
    score = {k: lexical.cosine("?", k) for k in texts}
    if embedder is not None:
        keys = list(texts)
        vectors = embedder.vectors([focus, *[texts[k] for k in keys]])
        for k, v in zip(keys, vectors[1:]):
            score[k] = 0.5 * score[k] + 0.5 * calibrated(cosine_of(vectors[0], v))
    return score


def _cut(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rsplit(" ", 1)[0] + "…"


def dossier(subgraphs: list[Subgraph], alignments: list[Alignment], leads: list[Lead], keys: dict[str, str],
            focus: str = "", embedder: Embedder | None = None) -> str:
    slugs = {sg.slug for sg in subgraphs}
    out: list[str] = ["# Dossier", ""]
    out += ["Everything the write-up may assert is here, stated once, with the citation key of the paper it rests on "
            "(`[@key]`, to be written `\\citep{key}`). A claim that is not here needs a source added with `scibraid draft cite`.", ""]
    if focus:
        out += [f"Focus: {focus}", "", "Hypotheses and leads are ordered by relevance to the focus. Nothing has been left out: "
                "the focus chooses the topic, not which evidence counts.", ""]

    # how the review was made
    out += ["## How this review was made", ""]
    for sg in subgraphs:
        passages = [p for item in [*sg.nodes.values(), *sg.edges] for p in item.provenance]
        verified = sum(p.verified is True for p in passages)
        body = sum(p.location != "abstract" for p in passages)
        closed = sum(p.oa_status == "closed" for p in sg.papers.values())
        notext = sum(store.load_text(pid) is None for pid in sg.papers)
        who = "; ".join(dict.fromkeys(" / ".join(x for x in (b.person, b.model, b.agent) if x) for b in sg.builders)) or "not recorded"
        out += [f"- `{sg.slug}`: {sg.question}",
                f"  - {len(sg.papers)} papers ({notext} read from the abstract only, {closed} closed), {len(sg.nodes)} nodes, {len(sg.edges)} links; "
                f"{verified} of {len(passages)} quoted passages verified against the source, {body} of them from the body of a paper",
                f"  - built by: {who}" + (f"; {len(sg.retracted)} retraction(s) on record" if sg.retracted else "")]
        unread = [pid for pid in sg.papers if store.load_text(pid) is None]
        if unread:
            out.append("  - read from the abstract only: " + ", ".join(f"[@{keys.get(pid, pid)}]" for pid in unread))
        shut = [pid for pid, paper in sg.papers.items() if paper.oa_status == "closed"]
        if shut:
            out.append("  - closed access: " + ", ".join(f"[@{keys.get(pid, pid)}]" for pid in shut))
        for r in sg.retracted:
            what = (f"node `{r.node.id}` ({_cut(r.node.label, 80)}) and {len(r.edges)} link(s)" if r.node
                    else "; ".join(f"`{e.source}` {e.relation.value} `{e.target}`" for e in r.edges))
            out.append(f"  - retracted: {what}. Reason: {_cut(r.reason, 260)}")
        if sg.brief:
            out.append(f"  - framed with this brief: {sg.brief}")
        if sg.prompted_by:
            out.append(f"  - a follow-up to lead `{sg.prompted_by}`")
    if len(subgraphs) > 1:
        pairs = [identity.relation(a.builders, b.builders) for i, a in enumerate(subgraphs) for b in subgraphs[i + 1:]]
        out.append(f"- readers across these subgraphs, weakest pairing: {identity.weakest(pairs)}")
    inside = [x for x in alignments if x.a.split("/")[0] in slugs and x.b.split("/")[0] in slugs]
    out += [f"- {len(inside)} alignment verdicts between these subgraphs", ""]

    index: dict[str, tuple[Subgraph, str]] = {}
    for sg in subgraphs:
        for nid in sg.nodes:
            index[f"{sg.slug}/{nid}"] = (sg, nid)

    def cite(paper_id: str) -> str:
        return f"[@{keys.get(paper_id, paper_id)}]"

    texts = {f"{sg.slug}/{nid}": f"{n.label} {n.description}" for sg in subgraphs for nid, n in sg.nodes.items() if n.type is NodeType.HYPOTHESIS}
    texts.update({f"lead:{lead.id}": f"{lead.claim} {lead.follow_up or ''}" for lead in leads})
    relevance = _relevance(focus, texts, embedder)

    def rel(key: str) -> str:
        return f" (relevance {relevance[key]:.2f})" if key in relevance else ""

    # hypotheses and their evidence
    out += ["## Hypotheses and the evidence for and against each", ""]
    ordered = sorted(subgraphs, key=lambda sg: -max([relevance.get(f"{sg.slug}/{nid}", 0) for nid, n in sg.nodes.items() if n.type is NodeType.HYPOTHESIS] or [0]))
    for sg in ordered:
        out += [f"### `{sg.slug}`", ""]
        under: dict[str, list[str]] = defaultdict(list)
        parent: dict[str, str] = {}
        for e in sg.edges:
            if e.relation in (Relation.PERFORMED_UNDER, Relation.OBSERVED_UNDER):
                under[e.source].append(sg.nodes[e.target].label)
            elif e.relation is Relation.YIELDS:
                parent[e.target] = e.source
        hypotheses = [nid for nid, n in sg.nodes.items() if n.type is NodeType.HYPOTHESIS]
        for nid in sorted(hypotheses, key=lambda h: -relevance.get(f"{sg.slug}/{h}", 0)):
            node = sg.nodes[nid]
            marks = [m for m, on in (("framed when the question was posed", node.framed), ("derived from a lead, not from a paper", node.derived_from)) if on]
            out.append(f"#### `{nid}`{rel(f'{sg.slug}/{nid}')}: {node.label}" + (f" [{'; '.join(marks)}]" if marks else ""))
            rivals = [sg.nodes[e.target if e.source == nid else e.source].label for e in sg.edges
                      if e.relation is Relation.COMPETES_WITH and nid in (e.source, e.target)]
            if rivals:
                out.append("Rivals: " + " | ".join(_cut(r, 110) for r in rivals))
            for relation in (Relation.SUPPORTS, Relation.CONTRADICTS):
                found = sorted((e for e in sg.edges if e.target == nid and e.relation is relation), key=lambda e: -e.confidence)
                papers = {e.provenance[0].paper_id for e in found}
                out.append(f"{relation.value.capitalize()}: {len(found)} observation(s) from {len(papers)} paper(s)")
                for e in found:
                    obs = sg.nodes[e.source]
                    conditions = under.get(e.source, []) + under.get(parent.get(e.source, ""), [])
                    drawn = "the paper draws the link" if e.asserted_by.value == "author" else "inferred by the reader, not stated in the paper"
                    out.append(f"- {cite(e.provenance[0].paper_id)} {obs.label} [{obs.outcome.value if obs.outcome else ''}; confidence {e.confidence:.2f}; {drawn}]")
                    if conditions:
                        out.append(f"  - under: {_cut('; '.join(dict.fromkeys(conditions)), 420)}")
                    out.append(f"  - passage: “{_cut(e.provenance[0].passage, 420)}”")
                    if e.note:
                        out.append(f"  - note: {_cut(e.note, 300)}")
            proposed = [e for e in sg.edges if e.relation is Relation.PROPOSES and e.target == nid]
            for e in proposed:
                out.append(f"- proposed as an explanation by {cite(e.provenance[0].paper_id)}: {sg.nodes[e.source].label}")
            out.append("")
        clashes = [e for e in sg.edges if e.relation is Relation.CONTRADICTS and sg.nodes[e.target].type is NodeType.OBSERVATION]
        if clashes:
            out += ["#### Results recorded as contradicting each other", ""]
            for e in clashes:
                a, b = sg.nodes[e.source], sg.nodes[e.target]
                pa = next((p.paper_id for p in a.provenance), e.provenance[0].paper_id)
                pb = next((p.paper_id for p in b.provenance), e.provenance[0].paper_id)
                out.append(f"- {cite(pa)} {a.label} AGAINST {cite(pb)} {b.label} [confidence {e.confidence:.2f}]" + (f" Note: {_cut(e.note, 300)}" if e.note else ""))
            out.append("")
        unused = [n.label for nid, n in sg.nodes.items() if n.framed and n.type is NodeType.CONDITION and not any(e.target == nid for e in sg.edges)]
        if unused:
            out += ["#### Framed conditions no experiment was recorded under", "",
                    "Either no paper found ran them or the extraction missed them. Say which only if the methods sections were checked.", ""]
            out += [f"- {label}" for label in unused] + [""]

    # links between the subgraphs' hypotheses
    links = [x for x in inside if x.verdict.value != "different" and "/h:" in x.a + x.b]
    if links:
        out += ["## How hypotheses in different subgraphs bear on each other", ""]
        for x in sorted(links, key=lambda x: -x.confidence):
            (sa, na), (sb, nb) = index.get(x.a, (None, None)), index.get(x.b, (None, None))
            if sa is None or sb is None:
                continue
            out += [f"- {x.verdict.value} ({x.confidence:.2f}): `{x.a}` {_cut(sa.nodes[na].label, 140)} ~ `{x.b}` {_cut(sb.nodes[nb].label, 140)}",
                    f"  - {x.rationale}"]
        out.append("")

    # leads
    touching = [lead for lead in leads if {k.split("/")[0] for k in lead.nodes} & slugs]
    if touching:
        out += ["## Leads: connections drawn from the pool, and what checking them found", "",
                "`holds` and `open` mean a brief search found nothing against them, not that nothing exists. `known` and `refuted` leads are results too.", ""]
        for lead in sorted(touching, key=lambda x: -relevance.get(f"lead:{x.id}", 0)):
            out.append(f"### `{lead.id}`{rel(f'lead:{lead.id}')}: {lead.status.value}, confidence {lead.confidence:.2f}")
            out += [lead.claim, ""]
            for check in lead.checks:
                sources = " ".join(cite(s) if s in keys else s for s in check.sources)
                out.append(f"- checked: {check.question} Found: {check.finding} {sources}".rstrip())
            for label, value in (("would confirm", lead.would_confirm), ("would refute", lead.would_refute), ("next question", lead.follow_up)):
                if value:
                    out.append(f"- {label}: {value}")
            if lead.known_in:
                out.append("- already in the literature: " + "; ".join(lead.known_in))
            if lead.posed_in is not None:
                out.append("- the question is posed in: " + ("; ".join(lead.posed_in) if lead.posed_in else "nowhere that a search found"))
            out.append("")

    # references
    out += ["## Citation keys", ""]
    papers = {pid: p for sg in subgraphs for pid, p in sg.papers.items()}
    for pid, key in sorted(keys.items(), key=lambda kv: kv[1]):
        paper = papers.get(pid) or store.load_paper(pid)
        if paper is not None:
            who = surname(paper.authors[0]) + (" et al." if len(paper.authors) > 1 else "") if paper.authors else "?"
            out.append(f"- `{key}`: {who} ({paper.year}) {_cut(paper.title, 110)} [{pid}]")
    return "\n".join(out) + "\n"


# ---- checking a draft -----------------------------------------------------------

_CITE = re.compile(r"\\cite[a-zA-Z]*\*?(?:\[[^\]]*\])*\{([^}]*)\}")
_QUOTE = re.compile(r"``(.+?)''|\\enquote\{(.+?)\}|\\begin\{quot(?:e|ation)\}(.+?)\\end\{quot(?:e|ation)\}", re.S)
_TEX = [(r"\\(?:emph|textit|textbf|textsc)\{([^}]*)\}", r"\1"), (r"\\([%&_#$])", r"\1"), (r"---", "—"), (r"--", "–"),
        (r"~", " "), (r"\\(?:ldots|dots)\b\{?\}?", "..."), (r"\\,|\\ ", " "), (r"\[\.\.\.\]", "...")]


def _plain(tex: str) -> str:
    for pattern, replacement in _TEX:
        tex = re.sub(pattern, replacement, tex)
    return " ".join(tex.split())


def _strip_comments(tex: str) -> str:
    return re.sub(r"(?<!\\)%.*", "", tex)


def check(directory: Path, compile_it: bool = True) -> dict:
    errors: list[str] = []
    warnings: list[str] = []
    main = directory / "main.tex"
    if not main.exists():
        return {"ok": False, "errors": [f"{main} does not exist"], "warnings": [], "tells": {}}
    raw = main.read_text()
    tex = _strip_comments(raw)
    state = json.loads((directory / "draft.json").read_text()) if (directory / "draft.json").exists() else {"keys": {}}
    keys: dict[str, str] = state.get("keys", {})
    by_key = {key: pid for pid, key in keys.items()}

    # citations
    cited = {k.strip() for group in _CITE.findall(tex) for k in group.split(",") if k.strip()}
    for key in sorted(cited - set(by_key)):
        errors.append(f"\\cite{{{key}}}: no such key in refs.bib. Add the paper with `scibraid draft cite`, never by hand")
    if unused := sorted(set(by_key) - cited):
        warnings.append(f"{len(unused)} of {len(by_key)} references are never cited; plainnat prints only cited ones, so this is a note on coverage: "
                        + ", ".join(unused[:12]) + (" ..." if len(unused) > 12 else ""))

    # quotations must be verbatim in a source cited in the same paragraph (or, failing that, in any source)
    for paragraph in re.split(r"\n\s*\n", tex):
        here = [by_key[k.strip()] for group in _CITE.findall(paragraph) for k in group.split(",") if k.strip() in by_key]
        for match in _QUOTE.finditer(paragraph):
            quote = _plain(next(g for g in match.groups() if g))
            if len(quote.split()) < 8:
                continue
            pool = here or list(keys)
            sources = [t for pid in pool for t in (store.load_text(pid), (store.load_paper(pid) or Paper(id="x", title="x")).abstract) if t]
            if not any(store.passage_in_text(quote, text) for text in sources):
                where = "the source(s) cited in its paragraph" if here else "any source held (and its paragraph cites none)"
                errors.append(f"quotation not found verbatim in {where}: “{_cut(quote, 120)}”")

    # what arXiv needs
    if "\\pdfoutput=1" not in "\n".join(raw.splitlines()[:5]):
        errors.append("\\pdfoutput=1 must be within the first five lines, or arXiv may not use pdflatex")
    if found := re.search(r"\\begin\{abstract\}(.*?)\\end\{abstract\}", tex, re.S):
        length = len(_plain(re.sub(r"\\cite[a-zA-Z]*\{[^}]*\}", "", found.group(1))))
        if length > ABSTRACT_LIMIT:
            errors.append(f"the abstract is {length} characters; arXiv's limit is {ABSTRACT_LIMIT}")
    else:
        errors.append("no abstract")
    for placeholder in ("TITLE", "AUTHOR NAME", "ABSTRACT", "TODO", "TBD"):
        if re.search(rf"(?<![A-Za-z]){re.escape(placeholder)}(?![A-Za-z])", tex):
            warnings.append(f"placeholder left in the text: {placeholder}")
    for kind, path in re.findall(r"\\(includegraphics|input|include)(?:\[[^\]]*\])?\{([^}]*)\}", tex):
        if path.startswith(("/", "~")) or ".." in path:
            errors.append(f"{path}: arXiv needs paths relative to the paper's own directory")
        elif not any((directory / (path + ext)).exists() for ext in ("", ".pdf", ".png", ".jpg", ".tex")):
            warnings.append(f"{path}: file not found next to main.tex")

    # prose
    tells: dict[str, list[str]] = {}
    body = re.sub(r"\\begin\{(?:table|tabular|figure|thebibliography)\}.*?\\end\{(?:table|tabular|figure|thebibliography)\}", "", tex, flags=re.S)
    lines = body.splitlines()
    for kind, phrases in TELLS.items():
        hits = [f"line {n}: {phrase.strip(' ,')}" for n, line in enumerate(lines, 1) for phrase in phrases if phrase in line.lower()]
        if hits:
            tells[kind] = hits
    for kind, pattern in (("em-dashes", r"---"), ("bold in running text", r"\\textbf\{"), ("rhetorical questions", r"\?(?:\s|$|'')")):
        hits = [f"line {n}" for n, line in enumerate(lines, 1) if re.search(pattern, line) and not line.lstrip().startswith("\\")]
        if hits:
            tells[kind] = hits
    long_headings = [f"line {n}: {m.group(1)}" for n, line in enumerate(lines, 1)
                     if (m := re.match(r"\s*\\(?:sub)*section\*?\{(.+)\}", line)) and (len(m.group(1).split()) > 5 or ":" in m.group(1))]
    if long_headings:
        tells["headings that explain themselves"] = long_headings

    compiled = None
    if compile_it and not errors:
        compiled = _compile(directory, errors, warnings)
    return {"ok": not errors, "errors": errors, "warnings": warnings, "tells": tells,
            "tell_count": sum(len(v) for v in tells.values()), "words": len(re.findall(r"[A-Za-z]{2,}", _plain(body))), "pdf": compiled}


def _compile(directory: Path, errors: list[str], warnings: list[str]) -> str | None:
    if not shutil.which("pdflatex"):
        warnings.append("pdflatex is not installed, so the draft was not compiled")
        return None
    steps = [["pdflatex", "-interaction=nonstopmode", "-halt-on-error", "main.tex"], ["bibtex", "main"],
             ["pdflatex", "-interaction=nonstopmode", "-halt-on-error", "main.tex"], ["pdflatex", "-interaction=nonstopmode", "-halt-on-error", "main.tex"]]
    for step in steps:
        if step[0] == "bibtex" and not shutil.which("bibtex"):
            warnings.append("bibtex is not installed; references were not resolved")
            continue
        try:
            done = subprocess.run(step, cwd=directory, capture_output=True, text=True, timeout=180, errors="replace")
        except subprocess.TimeoutExpired:
            errors.append(f"{step[0]} timed out")
            return None
        if done.returncode != 0 and step[0] == "pdflatex":
            problem = [line for line in done.stdout.splitlines() if line.startswith("!")][:3]
            errors.append("LaTeX did not compile: " + " ".join(problem or ["see main.log"]))
            return None
    log = (directory / "main.log").read_text(errors="replace") if (directory / "main.log").exists() else ""
    if undefined := sorted(set(re.findall(r"Citation `([^']+)' on page", log))):
        errors.append("citations LaTeX could not resolve: " + ", ".join(undefined))
    if missing := sorted(set(re.findall(r"Reference `([^']+)' on page", log))):
        errors.append("cross-references LaTeX could not resolve: " + ", ".join(missing))
    overfull = len(re.findall(r"Overfull \\hbox", log))
    if overfull:
        warnings.append(f"{overfull} overfull line(s); look at the PDF")
    if not (directory / "main.bbl").exists():
        warnings.append("no main.bbl was produced; arXiv needs it uploaded with the source")
    return str(directory / "main.pdf") if (directory / "main.pdf").exists() else None
