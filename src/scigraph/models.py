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
    """What the extracting agent hands to `scigraph add`: usually one paper's worth."""

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
