"""Local storage: paper cache, attached full text, and subgraph files."""

from __future__ import annotations

import fcntl
import os
import re
import tempfile
import unicodedata
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from .models import ALLOWED_ENDPOINTS, AssertedBy, Batch, Paper, Subgraph, _now


def home() -> Path:
    root = os.environ.get("SCIBRAID_HOME")
    path = Path(root) if root else Path.home() / ".local" / "share" / "scibraid"
    for sub in ("papers", "fulltext", "subgraphs"):
        (path / sub).mkdir(parents=True, exist_ok=True)
    return path


def _write_atomic(path: Path, text: str) -> None:
    """Write via a temporary file and rename, so a reader never sees a half-written file
    and two writers cannot interleave into invalid JSON."""
    handle, temp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(handle, "w") as out:
            out.write(text)
        os.replace(temp, path)
    except BaseException:
        Path(temp).unlink(missing_ok=True)
        raise


@contextmanager
def subgraph_lock(slug: str):
    """Hold while reading, changing and saving one subgraph. Several sessions may build
    different subgraphs freely; two adding to the same one would otherwise lose updates."""
    lock = home() / "subgraphs" / f".{slug}.lock"
    with open(lock, "w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _safe(paper_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9._\-]", "_", paper_id)


# --- papers ---------------------------------------------------------------


def save_paper(paper: Paper) -> None:
    _write_atomic(home() / "papers" / f"{_safe(paper.id)}.json", paper.model_dump_json(indent=2))


def load_paper(paper_id: str) -> Paper | None:
    path = home() / "papers" / f"{_safe(paper_id)}.json"
    return Paper.model_validate_json(path.read_text()) if path.exists() else None


def attach_text(paper_id: str, text: str) -> None:
    _write_atomic(home() / "fulltext" / f"{_safe(paper_id)}.txt", text)


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
        raise FileNotFoundError(f"no subgraph {slug!r}; create it with `scibraid new`")
    return Subgraph.model_validate_json(path.read_text())


def save_subgraph(sg: Subgraph) -> None:
    sg.updated = _now()
    _write_atomic(subgraph_path(sg.slug), sg.model_dump_json(indent=2))


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
        if edge.asserted_by is AssertedBy.MODEL and edge.confidence > 0.85:
            report.errors.append(
                f"{name}: model-asserted at {edge.confidence:.2f}, above 0.85. If the paper states the "
                "relationship itself (as it usually does for its own conditions and results) it is "
                "author-asserted; if you inferred it, no inference deserves more than 0.85"
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

    withdrawn = {(*e.key, e.provenance[0].paper_id): r.reason for r in sg.retracted for e in r.edges}
    for edge in batch.edges:
        if reason := withdrawn.get((*edge.key, edge.provenance[0].paper_id)):
            report.warnings.append(
                f"edge {edge.source} -{edge.relation}-> {edge.target}: this was retracted earlier ({reason}); adding it again"
            )

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


def merge_nodes(sg: Subgraph, keep: str, drop: str) -> list[str]:
    """Fold node `drop` into `keep`: one thing that was recorded under two ids.

    Edges move to `keep`. Where that makes two edges from the same paper say the same thing,
    they become one, holding both passages and the higher confidence. Returns the errors, and
    changes nothing if there are any.
    """
    missing = [nid for nid in (keep, drop) if nid not in sg.nodes]
    if missing:
        return [f"unknown node(s) {missing}"]
    if keep == drop:
        return ["a node cannot be merged into itself"]
    kept, dropped = sg.nodes[keep], sg.nodes[drop]
    if kept.type is not dropped.type:
        return [f"{keep} is a {kept.type} and {drop} is a {dropped.type}"]
    if kept.outcome is not dropped.outcome:
        return [f"{keep} is {kept.outcome} and {drop} is {dropped.outcome}: not one observation"]

    seen = {(p.paper_id, p.passage) for p in kept.provenance}
    kept.provenance += [p for p in dropped.provenance if (p.paper_id, p.passage) not in seen]
    kept.description = kept.description or dropped.description
    kept.framed = kept.framed or dropped.framed
    kept.attrs = {**dropped.attrs, **kept.attrs}
    kept.attrs["merged_from"] = [*kept.attrs.get("merged_from", []), *dropped.attrs.get("merged_from", []), drop]
    del sg.nodes[drop]

    merged: dict[tuple, int] = {}
    edges = []
    for edge in sg.edges:
        edge.source = keep if edge.source == drop else edge.source
        edge.target = keep if edge.target == drop else edge.target
        if edge.source == edge.target:
            continue  # a relation between the two ids says nothing once they are one node
        key = (*edge.key, edge.provenance[0].paper_id)
        if key not in merged:
            merged[key] = len(edges)
            edges.append(edge)
            continue
        first = edges[merged[key]]
        passages = {p.passage for p in first.provenance}
        first.provenance += [p for p in edge.provenance if p.passage not in passages]
        first.confidence = max(first.confidence, edge.confidence)
    sg.edges = edges
    return []


def reidentify_paper(old: str, paper: Paper) -> tuple[list[str], list[str]]:
    """Give a cached paper its proper record: every local subgraph that cites `old` cites `paper` instead.

    The full text moves with it, since recorded passages were checked against that text. If the new
    id already holds a different text, the passages must all be found in it, or nothing is changed.
    Returns (slugs rewritten, errors).
    """
    if old == paper.id:
        return [], [f"{old} already has that id"]
    citing = [sg for sg in list_subgraphs() if old in sg.papers]
    old_text, new_text = load_text(old), load_text(paper.id)
    if old_text is not None and new_text is not None and old_text != new_text:
        passages = {
            p.passage
            for sg in citing
            for item in [*sg.nodes.values(), *sg.edges]
            for p in item.provenance
            if p.paper_id == old and p.location != "abstract"
        }
        if lost := [q for q in passages if not passage_in_text(q, new_text)]:
            return [], [f"{paper.id} already holds a full text in which {len(lost)} recorded passage(s) are not found, e.g. {lost[0][:80]!r}"]
    elif old_text is not None:
        attach_text(paper.id, old_text)
        paper.text_source = paper.text_source or (load_paper(old) or paper).text_source
    # Passages quoted from the abstract were checked against the old record's wording of it.
    if (held := load_paper(old)) is not None and held.abstract:
        quoted = {
            p.passage
            for sg in citing
            for item in [*sg.nodes.values(), *sg.edges]
            for p in item.provenance
            if p.paper_id == old and p.location == "abstract"
        }
        if not paper.abstract or not all(passage_in_text(q, paper.abstract) for q in quoted):
            paper.abstract = held.abstract
    save_paper(paper)
    for sg in citing:
        with subgraph_lock(sg.slug):
            sg = load_subgraph(sg.slug)
            del sg.papers[old]
            sg.papers[paper.id] = paper
            for item in [*sg.nodes.values(), *sg.edges]:
                for prov in item.provenance:
                    if prov.paper_id == old:
                        prov.paper_id = paper.id
            save_subgraph(sg)
    for folder, suffix in (("papers", ".json"), ("fulltext", ".txt")):
        (home() / folder / f"{_safe(old)}{suffix}").unlink(missing_ok=True)
    return [sg.slug for sg in citing], []


_FRAME_ID = re.compile(r"^([hc]:[a-z0-9][a-z0-9\-]*)=(.+)$", re.S)


def _frame_node(prefix: str, text: str) -> tuple[str, str]:
    """(id, label) from "Label", or from "h:my-id=Label" when the id matters."""
    if found := _FRAME_ID.match(text.strip()):
        return found.group(1), found.group(2).strip()
    words = re.findall(r"[a-z0-9]+", text.lower())
    slug = ""
    for word in words:
        if len(slug) + len(word) > 60:
            break
        slug = f"{slug}-{word}" if slug else word
    return f"{prefix}:{slug}", text.strip()


def frame(sg: Subgraph, hypotheses: list[str] = (), conditions: list[str] = (), brief: str | None = None) -> list[str]:
    """Record how a question was posed: hypotheses to test, conditions to keep apart, and a brief.

    Returns errors, and changes nothing if there are any. Framing a node that exists marks it.
    """
    from .models import Node, NodeType

    wanted = [(*_frame_node("h", t), NodeType.HYPOTHESIS) for t in hypotheses]
    wanted += [(*_frame_node("c", t), NodeType.CONDITION) for t in conditions]
    errors = []
    for nid, label, kind in wanted:
        if not nid.startswith(f"{kind.value[0]}:"):
            errors.append(f"{nid!r} is given as a {kind.value} but its id says otherwise")
        elif len(nid) < 4 or not label:
            errors.append(f"{label or nid!r}: nothing to make an id or a label from")
        elif len(label) > 200:
            errors.append(f"{nid}: a label is at most 200 characters; put the rest in the brief")
        elif nid in sg.nodes and sg.nodes[nid].type is not kind:
            errors.append(f"{nid} already exists as a {sg.nodes[nid].type}")
    if errors:
        return errors
    for nid, label, kind in wanted:
        if nid in sg.nodes:
            sg.nodes[nid].framed = True
        else:
            sg.nodes[nid] = Node(id=nid, type=kind, label=label, framed=True)
    if brief is not None:
        sg.brief = brief.strip()
    return []


def retract(sg: Subgraph, reason: str, by=None, node: str | None = None, edge: tuple[str, str, str] | None = None, paper: str | None = None) -> tuple[int, list[str]]:
    """Take a wrong node (with its edges) or a wrong edge out of a subgraph, keeping a record of it.

    Returns (edges removed, errors). An edge is named by source, relation and target, and by paper
    when more than one paper evidences it.
    """
    from .models import Retraction

    if (node is None) == (edge is None):
        return 0, ["name a node or an edge, not both"]
    if node is not None:
        if node not in sg.nodes:
            return 0, [f"unknown node {node!r}"]
        gone = [e for e in sg.edges if node in (e.source, e.target)]
        record = Retraction(reason=reason, node=sg.nodes.pop(node), edges=gone, by=by)
    else:
        gone = [e for e in sg.edges if e.key == tuple(edge) and paper in (None, e.provenance[0].paper_id)]
        if not gone:
            return 0, [f"no edge {edge[0]} -{edge[1]}-> {edge[2]}" + (f" from {paper}" if paper else "")]
        if len(gone) > 1:
            papers = sorted(e.provenance[0].paper_id for e in gone)
            return 0, [f"{len(gone)} papers evidence that edge ({', '.join(papers)}); say which with --paper"]
        record = Retraction(reason=reason, edges=gone, by=by)
    sg.edges = [e for e in sg.edges if all(e is not g for g in gone)]
    sg.retracted.append(record)
    return len(gone), []
