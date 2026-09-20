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


class Builder(BaseModel):
    """Who did a piece of work. Any field may be unknown."""

    model_config = ConfigDict(extra="forbid")

    person: str | None = None
    agent: str | None = None  # the harness, e.g. claude-code
    model: str | None = None  # the language model doing the reading and judging
    session: str | None = None


class Derivation(BaseModel):
    """Where a hypothesis came from when it came from the pool rather than from a paper."""

    model_config = ConfigDict(extra="forbid")

    lead: str
    confidence: float = Field(ge=0.0, le=1.0)
    nodes: list[str] = []  # <slug>/<node id> keys the lead rested on
    alignments: list[tuple[str, str]] = []


class Node(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[a-z0-9][a-z0-9:_\-\.]*$")
    type: NodeType
    label: str = Field(min_length=1, max_length=200)
    description: str = ""
    outcome: Outcome | None = None
    attrs: dict[str, Any] = {}
    provenance: list[Provenance] = []
    # Set only on a hypothesis that a lead proposed. It has no standing of its own: it is a
    # claim to be tested, and gains support only from paper evidence linked to it.
    derived_from: Derivation | None = None

    @model_validator(mode="after")
    def _outcome_only_on_observations(self) -> Node:
        if self.derived_from is not None and self.type is not NodeType.HYPOTHESIS:
            raise ValueError(f"{self.id!r}: only a hypothesis can be derived from a lead")
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
    prompted_by: str | None = None  # id of the lead whose follow-up this subgraph answers
    builders: list[Builder] = []  # everyone who has added to it, in order of first contribution
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
    judge: Builder | None = None  # set by the tool when the verdict is recorded

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
    HOLDS = "holds"  # premise verified, no confound found, not found in a brief search: provisional
    # An open research question: the literature has been reviewed and does not settle it, so
    # only new empirical work can. This is where the loop stops and hands over to a person.
    OPEN = "open"
    KNOWN = "known"  # holds, but the literature already makes the connection
    REFUTED = "refuted"  # the premise fails or a confound explains it


class Check(BaseModel):
    """One thing that was checked about a lead, and what was found."""

    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=10)
    finding: str = Field(min_length=10)
    sources: list[str] = []  # paper ids or URLs consulted


class Repair(BaseModel):
    """A fault in a subgraph or a verdict, found while checking a lead, for its owner to fix."""

    model_config = ConfigDict(extra="forbid")

    kind: str = Field(pattern=r"^(extraction|alignment)$")
    target: str = Field(min_length=1, description="a subgraph slug, a <slug>/<node id> key, or 'a ~ b' for a verdict")
    problem: str = Field(min_length=10)
    resolved: bool = False
    resolution: str = ""


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
    # Where the literature poses the question without answering it. None means nobody looked;
    # an empty list means a search found it posed nowhere. Open is not the same as new.
    posed_in: list[str] | None = None
    follow_up: str | None = None  # a question to hand back to evidence-subgraph
    repairs: list[Repair] = []
    by: Builder | None = None  # who last checked and recorded it; set by the tool
    updated: str = Field(default_factory=_now)

    @model_validator(mode="after")
    def _checked_before_judged(self) -> Lead:
        if self.status is not LeadStatus.CANDIDATE and not self.checks:
            raise ValueError(f"lead {self.id!r} is {self.status.value} but records no checks")
        if self.status is LeadStatus.KNOWN and not self.known_in:
            raise ValueError(f"lead {self.id!r} is known: say where, in known_in")
        if self.status in (LeadStatus.HOLDS, LeadStatus.OPEN) and not (self.would_confirm and self.would_refute):
            raise ValueError(
                f"lead {self.id!r} is {self.status.value}: say what would confirm and what would refute it"
            )
        return self
