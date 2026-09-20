"""Local storage: paper cache, attached full text, and subgraph files."""

from __future__ import annotations

import os
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

from .models import ALLOWED_ENDPOINTS, Batch, Paper, Subgraph, _now


def home() -> Path:
    root = os.environ.get("SUBGRAFT_HOME")
    path = Path(root) if root else Path.home() / ".local" / "share" / "subgraft"
    for sub in ("papers", "fulltext", "subgraphs"):
        (path / sub).mkdir(parents=True, exist_ok=True)
    return path


def _safe(paper_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9._\-]", "_", paper_id)


# --- papers ---------------------------------------------------------------


def save_paper(paper: Paper) -> None:
    (home() / "papers" / f"{_safe(paper.id)}.json").write_text(paper.model_dump_json(indent=2))


def load_paper(paper_id: str) -> Paper | None:
    path = home() / "papers" / f"{_safe(paper_id)}.json"
    return Paper.model_validate_json(path.read_text()) if path.exists() else None


def attach_text(paper_id: str, text: str) -> None:
    (home() / "fulltext" / f"{_safe(paper_id)}.txt").write_text(text)


def load_text(paper_id: str) -> str | None:
    path = home() / "fulltext" / f"{_safe(paper_id)}.txt"
    return path.read_text() if path.exists() else None


# --- passage verification -------------------------------------------------

_ELLIPSIS = re.compile(r"\s*(?:\[\s*(?:\.\.\.|…)\s*\]|\.\.\.|…)\s*")
_PUNCT = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"', "–": "-", "—": "-", "−": "-"})


def _norm(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).translate(_PUNCT)
    return re.sub(r"\s+", " ", text).strip().casefold()


def _find(haystack: str, fragment: str, start: int, whole_words: bool) -> int:
    """Index of the first occurrence at or after start, optionally only on word boundaries."""
    while (found := haystack.find(fragment, start)) >= 0:
        end = found + len(fragment)
        cut_start = fragment[0].isalnum() and found > 0 and haystack[found - 1].isalnum()
        cut_end = fragment[-1].isalnum() and end < len(haystack) and haystack[end].isalnum()
        if not whole_words or not (cut_start or cut_end):
            return found
        start = found + 1
    return -1


def passage_in_text(passage: str, text: str, whole_words: bool = True) -> bool:
    """True if the passage is a verbatim quote of the text.

    Whitespace, case and quote/dash style are ignored. An ellipsis in the passage
    may elide text, but the fragments must appear in order. Each fragment must
    start and end on a word boundary of the text: a quote cut off mid-word is a
    sign it was copied from truncated output rather than read.
    """
    haystack = _norm(text)
    pos = 0
    fragments = [f for f in (_norm(p) for p in _ELLIPSIS.split(passage)) if f]
    if not fragments:
        return False
    for fragment in fragments:
        found = _find(haystack, fragment, pos, whole_words)
        if found < 0:
            return False
        pos = found + len(fragment)
    return True


def cut_mid_word(paper_id: str, passage: str) -> bool:
    """True if the passage fails verification only because it starts or ends mid-word."""
    paper = load_paper(paper_id)
    sources = [paper.abstract if paper else None, load_text(paper_id)]
    return any(s and passage_in_text(passage, s, whole_words=False) for s in sources)


def verify(paper_id: str, passage: str, location: str) -> bool | None:
    """True/False if we hold text to check against, None if we hold nothing."""
    paper = load_paper(paper_id)
    abstract = paper.abstract if paper else None
    text = load_text(paper_id)
    in_abstract = bool(abstract) and passage_in_text(passage, abstract)
    in_text = text is not None and passage_in_text(passage, text)
    if in_abstract or in_text:
        return True
    # A miss only counts when we hold the text the passage claims to come from.
    held = abstract if location == "abstract" and abstract else text
    return None if held is None else False


# --- subgraphs ------------------------------------------------------------


def subgraph_path(slug: str) -> Path:
    return home() / "subgraphs" / f"{slug}.json"


def load_subgraph(slug: str) -> Subgraph:
    path = subgraph_path(slug)
    if not path.exists():
        raise FileNotFoundError(f"no subgraph {slug!r}; create it with `subgraft new`")
    return Subgraph.model_validate_json(path.read_text())


def save_subgraph(sg: Subgraph) -> None:
    sg.updated = _now()
    subgraph_path(sg.slug).write_text(sg.model_dump_json(indent=2))


def list_subgraphs() -> list[Subgraph]:
    paths = sorted((home() / "subgraphs").glob("*.json"))
    return [Subgraph.model_validate_json(p.read_text()) for p in paths]


@dataclass
class AddReport:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    nodes_added: int = 0
    nodes_merged: int = 0
    edges_added: int = 0
    edges_replaced: int = 0

    @property
    def ok(self) -> bool:
        return not self.errors


def add_batch(sg: Subgraph, batch: Batch) -> AddReport:
    """Validate a batch against the subgraph and apply it. All-or-nothing."""
    report = AddReport()
    types = {nid: n.type for nid, n in sg.nodes.items()}

    for node in batch.nodes:
        if node.id in types and types[node.id] is not node.type:
            report.errors.append(
                f"node {node.id!r} already exists as {types[node.id]}, not {node.type}"
            )
        types.setdefault(node.id, node.type)

    for edge in batch.edges:
        name = f"edge {edge.source} -{edge.relation}-> {edge.target}"
        missing = [end for end in (edge.source, edge.target) if end not in types]
        if missing:
            report.errors.append(f"{name}: unknown node(s) {missing}")
            continue
        pair = (types[edge.source], types[edge.target])
        if pair not in ALLOWED_ENDPOINTS[edge.relation]:
            allowed = ", ".join(f"{s}->{t}" for s, t in sorted(ALLOWED_ENDPOINTS[edge.relation]))
            report.errors.append(
                f"{name}: {edge.relation} cannot link {pair[0]}->{pair[1]} (allowed: {allowed})"
            )
        if len({p.paper_id for p in edge.provenance}) > 1:
            report.errors.append(
                f"{name}: one edge is one paper's evidence; split it into an edge per paper"
            )

    papers: dict[str, Paper] = {}
    items = [(f"node {n.id}", n.provenance) for n in batch.nodes]
    items += [(f"edge {e.source} -{e.relation}-> {e.target}", e.provenance) for e in batch.edges]
    for name, provenance in items:
        for prov in provenance:
            paper = load_paper(prov.paper_id)
            if paper is None:
                report.errors.append(
                    f"{name}: paper {prov.paper_id!r} is not cached (search or `paper add` first)"
                )
                continue
            papers[paper.id] = paper
            prov.verified = verify(prov.paper_id, prov.passage, prov.location)
            if prov.verified is False:
                problem = (
                    "passage starts or ends mid-word; quote whole words from"
                    if cut_mid_word(prov.paper_id, prov.passage)
                    else "passage is not a verbatim quote of"
                )
                shown = prov.passage if len(prov.passage) <= 90 else (
                    f"{prov.passage[:45]} ... {prov.passage[-40:]}"
                )
                report.errors.append(f"{name}: {problem} {prov.paper_id}: {shown!r}")
            elif prov.verified is None:
                report.warnings.append(f"{name}: no text held for {prov.paper_id}; passage unverified")

    if report.errors:
        return report

    sg.papers.update(papers)
    for node in batch.nodes:
        if (existing := sg.nodes.get(node.id)) is None:
            sg.nodes[node.id] = node
            report.nodes_added += 1
            continue
        seen = {(p.paper_id, p.passage) for p in existing.provenance}
        existing.provenance += [p for p in node.provenance if (p.paper_id, p.passage) not in seen]
        existing.description = existing.description or node.description
        existing.attrs = {**node.attrs, **existing.attrs}
        report.nodes_merged += 1

    for edge in batch.edges:
        paper_id = edge.provenance[0].paper_id
        same = [
            i
            for i, e in enumerate(sg.edges)
            if e.key == edge.key and e.provenance[0].paper_id == paper_id
        ]
        if same:
            sg.edges[same[0]] = edge
            report.edges_replaced += 1
        else:
            sg.edges.append(edge)
            report.edges_added += 1
    return report
