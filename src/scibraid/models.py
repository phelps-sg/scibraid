"""Schema for evidence subgraphs.

The unit of knowledge is the experimentally grounded relationship, not the paper.
Every edge carries a confidence and at least one provenance record pointing at a
verbatim passage, so that model interpretations never silently become facts.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


class NodeType(StrEnum):
    HYPOTHESIS = "hypothesis"
    EXPERIMENT = "experiment"
    CONDITION = "condition"
    OBSERVATION = "observation"
    INTERPRETATION = "interpretation"


class Outcome(StrEnum):
    """What an observation showed relative to what the experiment was looking for."""

    POSITIVE = "positive"
    NEGATIVE = "negative"
    NULL = "null"
    INCONCLUSIVE = "inconclusive"
    MIXED = "mixed"


class Relation(StrEnum):
    TESTS = "tests"
    PERFORMED_UNDER = "performed_under"
    YIELDS = "yields"
    OBSERVED_UNDER = "observed_under"
    SUPPORTS = "supports"
    CONTRADICTS = "contradicts"
    EXPLAINS = "explains"
    PROPOSES = "proposes"
    COMPETES_WITH = "competes_with"
    FAILS_TO_REPLICATE = "fails_to_replicate"


H, E, C, O, I = (
    NodeType.HYPOTHESIS,
    NodeType.EXPERIMENT,
    NodeType.CONDITION,
    NodeType.OBSERVATION,
    NodeType.INTERPRETATION,
)

# Allowed (source type, target type) pairs per relation.
ALLOWED_ENDPOINTS: dict[Relation, set[tuple[NodeType, NodeType]]] = {
    Relation.TESTS: {(E, H)},
    Relation.PERFORMED_UNDER: {(E, C)},
    Relation.YIELDS: {(E, O)},
    # A condition scoping one result rather than the whole experiment: a subgroup or moderator.
    Relation.OBSERVED_UNDER: {(O, C)},
    Relation.SUPPORTS: {(O, H)},
    Relation.CONTRADICTS: {(O, H), (O, O)},
    Relation.EXPLAINS: {(I, O)},
    Relation.PROPOSES: {(I, H)},
    Relation.COMPETES_WITH: {(H, H)},
    Relation.FAILS_TO_REPLICATE: {(E, E)},
}


class AssertedBy(StrEnum):
    """Who drew the relationship: the paper's authors, or the extracting model."""

    AUTHOR = "author"
    MODEL = "model"


class SourceTier(StrEnum):
    PUBLISHED = "published"
    GREY = "grey"
    PROCESS = "process"


class Provenance(BaseModel):
    model_config = ConfigDict(extra="forbid")

    paper_id: str
    passage: str = Field(min_length=1, description="Verbatim quote from the source.")
    location: str = "abstract"
    # Set by the store, never by the extractor: did the passage match the cached text?
    verified: bool | None = None


class Paper(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    title: str
    doi: str | None = None
    year: int | None = None
    authors: list[str] = []
    venue: str | None = None
    url: str | None = None
    cited_by_count: int | None = None
    source_tier: SourceTier = SourceTier.PUBLISHED
    abstract: str | None = None


class Node(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[a-z0-9][a-z0-9:_\-\.]*$")
    type: NodeType
    label: str = Field(min_length=1, max_length=200)
    description: str = ""
    outcome: Outcome | None = None
    attrs: dict[str, Any] = {}
    provenance: list[Provenance] = []

    @model_validator(mode="after")
    def _outcome_only_on_observations(self) -> Node:
        if self.type is NodeType.OBSERVATION and self.outcome is None:
            raise ValueError(f"observation {self.id!r} needs an outcome")
        if self.type is not NodeType.OBSERVATION and self.outcome is not None:
            raise ValueError(f"{self.type} {self.id!r} cannot have an outcome")
        return self


class Edge(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: str
    target: str
    relation: Relation
    confidence: float = Field(ge=0.0, le=1.0)
    asserted_by: AssertedBy
    provenance: list[Provenance] = Field(min_length=1)
    note: str = ""

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.source, self.relation.value, self.target)


class Batch(BaseModel):
    """What the extracting agent hands to `scibraid add`: usually one paper's worth."""

    model_config = ConfigDict(extra="forbid")

    nodes: list[Node] = []
    edges: list[Edge] = []


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Subgraph(BaseModel):
    model_config = ConfigDict(extra="forbid")

    slug: str = Field(pattern=r"^[a-z0-9][a-z0-9\-]*$")
    question: str
    created: str = Field(default_factory=_now)
    updated: str = Field(default_factory=_now)
    papers: dict[str, Paper] = {}
    nodes: dict[str, Node] = {}
    edges: list[Edge] = []


class Verdict(StrEnum):
    """The judged relationship between two nodes from different subgraphs."""

    SAME = "same"
    BROADER = "broader"  # a is broader than b
    NARROWER = "narrower"  # a is narrower than b
    RELATED = "related"
    DIFFERENT = "different"


class Alignment(BaseModel):
    """A judgement linking nodes across subgraphs. Nodes are never merged: the link
    carries its own confidence and rationale, so it can be inspected and revised."""

    model_config = ConfigDict(extra="forbid")

    a: str = Field(pattern=r"^[a-z0-9\-]+/.+$", description="<slug>/<node id>")
    b: str = Field(pattern=r"^[a-z0-9\-]+/.+$")
    verdict: Verdict
    confidence: float = Field(ge=0.0, le=1.0)
    rationale: str = Field(min_length=10)
    judged: str = Field(default_factory=_now)

    @model_validator(mode="after")
    def _canonical_order(self) -> Alignment:
        if self.a.split("/", 1)[0] == self.b.split("/", 1)[0]:
            raise ValueError(f"{self.a} and {self.b} are in the same subgraph")
        if self.a > self.b:
            self.a, self.b = self.b, self.a
            flip = {Verdict.BROADER: Verdict.NARROWER, Verdict.NARROWER: Verdict.BROADER}
            self.verdict = flip.get(self.verdict, self.verdict)
        return self


class LeadKind(StrEnum):
    """Which reading of the pool suggested the lead."""

    BRIDGE = "bridge"
    SHARED_FAILURE = "shared_failure"
    CROSS_BEARING = "cross_bearing"
    REGIME = "regime"
    UNTESTED = "untested"
    OTHER = "other"


class LeadStatus(StrEnum):
    CANDIDATE = "candidate"  # read off the pool, not yet checked
    HOLDS = "holds"  # premise verified, no confound found, not found in the literature
    KNOWN = "known"  # holds, but the literature already makes the connection
    REFUTED = "refuted"  # the premise fails or a confound explains it


class Check(BaseModel):
    """One thing that was checked about a lead, and what was found."""

    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=10)
    finding: str = Field(min_length=10)
    sources: list[str] = []  # paper ids or URLs consulted


class Lead(BaseModel):
    """A candidate observation drawn from the pooled structure, kept with what it rests on.

    A lead is what the pool is for, so it is stored as carefully as a link: the nodes
    and alignment verdicts behind it, the checks made, and what would settle it.
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[a-z0-9][a-z0-9\-]*$")
    claim: str = Field(min_length=20)
    kind: LeadKind
    status: LeadStatus = LeadStatus.CANDIDATE
    confidence: float = Field(ge=0.0, le=1.0)
    nodes: list[str] = Field(min_length=2, description="<slug>/<node id> keys the lead rests on")
    alignments: list[tuple[str, str]] = []  # (a, b) pairs of the verdicts it rests on
    checks: list[Check] = []
    would_confirm: str = ""
    would_refute: str = ""
    known_in: list[str] = []  # where the literature already says it
    follow_up: str | None = None  # a question to hand back to evidence-subgraph
    updated: str = Field(default_factory=_now)

    @model_validator(mode="after")
    def _checked_before_judged(self) -> Lead:
        if self.status is not LeadStatus.CANDIDATE and not self.checks:
            raise ValueError(f"lead {self.id!r} is {self.status.value} but records no checks")
        if self.status is LeadStatus.KNOWN and not self.known_in:
            raise ValueError(f"lead {self.id!r} is known: say where, in known_in")
        if self.status is LeadStatus.HOLDS and not (self.would_confirm and self.would_refute):
            raise ValueError(f"lead {self.id!r} holds: say what would confirm and what would refute it")
        return self
