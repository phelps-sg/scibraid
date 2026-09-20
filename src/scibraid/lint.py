"""Structural checks on a subgraph: what an extractor should look at again."""

from __future__ import annotations

from collections import Counter

from .models import AssertedBy, NodeType, Outcome, Relation, Subgraph


def lint(sg: Subgraph) -> list[str]:
    findings: list[str] = []
    out: dict[str, set[Relation]] = {nid: set() for nid in sg.nodes}
    into: dict[str, set[Relation]] = {nid: set() for nid in sg.nodes}
    for edge in sg.edges:
        out[edge.source].add(edge.relation)
        into[edge.target].add(edge.relation)

    for nid, node in sg.nodes.items():
        if not out[nid] and not into[nid]:
            findings.append(f"{nid}: orphan {node.type}, no edges")
            continue
        if node.type is NodeType.EXPERIMENT:
            if Relation.YIELDS not in out[nid]:
                findings.append(f"{nid}: experiment yields no observation")
            if Relation.PERFORMED_UNDER not in out[nid]:
                findings.append(
                    f"{nid}: experiment has no conditions (conditions are what alignment keys on)"
                )
        elif node.type is NodeType.OBSERVATION and Relation.YIELDS not in into[nid]:
            findings.append(f"{nid}: observation comes from no experiment")
        elif node.type is NodeType.HYPOTHESIS and not (
            into[nid] & {Relation.SUPPORTS, Relation.CONTRADICTS}
        ):
            findings.append(f"{nid}: hypothesis has no direct evidence for or against")

    for edge in sg.edges:
        if edge.asserted_by is AssertedBy.MODEL and edge.confidence > 0.85:
            findings.append(
                f"{edge.source} -{edge.relation}-> {edge.target}: "
                f"model-asserted at {edge.confidence:.2f}; is that calibrated?"
            )

    provenance = [p for n in sg.nodes.values() for p in n.provenance]
    provenance += [p for e in sg.edges for p in e.provenance]
    if unverified := sum(1 for p in provenance if p.verified is not True):
        findings.append(f"{unverified} of {len(provenance)} passages are unverified")

    outcomes = Counter(n.outcome for n in sg.nodes.values() if n.outcome)
    failures = outcomes[Outcome.NEGATIVE] + outcomes[Outcome.NULL] + outcomes[Outcome.INCONCLUSIVE]
    if outcomes and not failures:
        findings.append(
            "no negative, null or inconclusive observations: search for failed replications, "
            "null results and boundary conditions before treating this as complete"
        )
    if len(sg.papers) == 1:
        findings.append("only one paper contributes; a subgraph should triangulate sources")
    return findings
