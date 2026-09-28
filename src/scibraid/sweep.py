"""The forward sweep: what has been published since, about the papers a subgraph rests on.

A result in a fast field is overturned by a later paper that reran it, not by one that calls itself
a replication, so the sweep looks among the works that cite each load-bearing paper: the newest of
them, and those whose title or abstract speaks of failure. It is bounded by construction: a few
papers, a few short queries each, newest first, so a paper with two thousand citations costs the
same as one with twenty.
"""

from __future__ import annotations

from collections import Counter
from typing import Callable

from . import openalex
from .models import NodeType, Paper, Relation, Subgraph, _now

# Words a later paper uses when a result did not hold. Each is one query among the citing works.
FAILURE_QUERIES = ("replication", "fails", "not", "null result", "revisit", "contradict")
NEWEST = "newest"


def load_bearing(sg: Subgraph, top: int = 5) -> list[tuple[str, int]]:
    """Papers ranked by how much of the graph rests on them: edges evidenced, counting an edge into
    a framed hypothesis twice, since those are the claims the question turns on."""
    weight: Counter[str] = Counter()
    for edge in sg.edges:
        target = sg.nodes.get(edge.target)
        framed = target is not None and target.framed and target.type is NodeType.HYPOTHESIS
        for prov in edge.provenance:
            weight[prov.paper_id] += 2 if framed and edge.relation in (Relation.SUPPORTS, Relation.CONTRADICTS) else 1
    return [(pid, n) for pid, n in weight.most_common() if pid in sg.papers][:top]


def sweep(sg: Subgraph, paper_ids: list[str], limit: int = 10, from_year: int | None = None,
          queries: tuple[str, ...] = (), searcher: Callable[..., list[Paper]] = openalex.search) -> list[dict]:
    """Candidates newest first: works citing the given papers that are not yet in the subgraph."""
    found: dict[str, dict] = {}
    for pid in paper_ids:
        if not pid.startswith("W"):
            continue  # a hand-added source has no citation graph to follow
        runs = [(NEWEST, None, "publication_date:desc")] + [(q, q, "publication_date:desc") for q in (*FAILURE_QUERIES, *queries)]
        for name, query, sort in runs:
            for paper in searcher(query, limit=limit, from_year=from_year, sort=sort, citing=pid):
                if paper.id in sg.papers or paper.id == pid:
                    continue
                entry = found.setdefault(paper.id, {"paper": paper, "hits": set(), "cites": set()})
                entry["hits"].add(name)
                entry["cites"].add(pid)
        sg.swept[pid] = _now()
    out = []
    for entry in found.values():
        p = entry["paper"]
        out.append({"id": p.id, "year": p.year, "cited_by_count": p.cited_by_count, "title": p.title,
                    "authors": p.authors[:3], "oa_status": p.oa_status, "is_retracted": p.is_retracted,
                    "hits": sorted(entry["hits"], key=lambda h: (h != NEWEST, h)), "cites": sorted(entry["cites"]), "_paper": p})
    out.sort(key=lambda c: (-(c["year"] or 0), -len(c["hits"]), -(c["cited_by_count"] or 0)))
    return out


def unswept(sg: Subgraph, top: int = 5) -> list[str]:
    return [pid for pid, _ in load_bearing(sg, top) if pid.startswith("W") and pid not in sg.swept]
