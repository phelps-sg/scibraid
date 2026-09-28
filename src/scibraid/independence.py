"""How many independent sources a relation rests on.

Three papers from one laboratory are one source reporting three times. Papers are grouped by shared
authorship: two papers with an author in common, by OpenAlex author id where the record has one and
by name otherwise, fall in one group, and groups chain (A with B, B with C). A group is a research
group in the loose sense that matters here: people who would not count as replicating one another.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Iterable

from .models import Paper, Relation, Subgraph


def _people(paper: Paper) -> set[str]:
    """Every key an author is known by: the id where there is one, and always the name, so that a
    record without ids still meets the same person in a record that has them."""
    ids = list(paper.author_ids) + [None] * (len(paper.authors) - len(paper.author_ids))
    keys = {"name:" + " ".join(name.lower().split()) for name in paper.authors}
    return keys | {aid for aid in ids if aid}


def groups(papers: Iterable[Paper]) -> dict[str, int]:
    """paper id -> group number, numbered in order of first appearance."""
    papers = list(papers)
    parent = {p.id: p.id for p in papers}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    by_person: dict[str, str] = {}
    for paper in papers:
        for person in _people(paper):
            if person in by_person:
                parent[find(paper.id)] = find(by_person[person])
            else:
                by_person[person] = paper.id
    numbers: dict[str, int] = {}
    return {p.id: numbers.setdefault(find(p.id), len(numbers) + 1) for p in papers}


def evidence_for(sg: Subgraph, target: str, relation: Relation) -> tuple[int, int, int]:
    """(edges, papers, independent groups) evidencing a relation into a node."""
    edges = [e for e in sg.edges if e.target == target and e.relation is relation]
    ids = {e.provenance[0].paper_id for e in edges}
    grouped = groups(sg.papers[pid] for pid in ids if pid in sg.papers)
    return len(edges), len(ids), len(set(grouped.values()))
