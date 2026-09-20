"""Alignment across pooled subgraphs, in two stages, then observation.

Stage one (here, cheap): propose node pairs from different subgraphs that might be
the same thing, scored by how plausible the match is and how much it would change
the pooled structure if true. Stage two (the agent, expensive) judges only the
pairs this stage ranks highest. Uncertainty is cheap to detect and expensive to
resolve, so the budget goes where resolving it matters.

`observe` then reads structure off the aligned pool. Everything it reports is a
candidate observation with the confidence and provenance of the links behind it,
never an asserted conclusion.
"""

from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from itertools import combinations

from .models import Alignment, Edge, Lead, Node, NodeType, Outcome, Relation, Subgraph, Verdict

STOPWORDS = frozenset(
    "a an and are as at be by for from in is it its of on or that the their this to under "
    "with without et al vs than then which when only not no can does do".split()
)
# Ids carry a type prefix (c:, h:, ...) that says nothing about what the node is.
_ID_PREFIX = re.compile(r"^[a-z]:")
FAILURES = {Outcome.NEGATIVE, Outcome.NULL, Outcome.INCONCLUSIVE}


def _tokens(text: str) -> list[str]:
    words = re.findall(r"[a-z0-9]+", text.lower())
    # Crude plural folding is enough to match "models"/"model", "tasks"/"task".
    return [w[:-1] if len(w) > 3 and w.endswith("s") else w for w in words if w not in STOPWORDS]


def _trigrams(text: str) -> set[str]:
    text = " ".join(_tokens(text))
    return {text[i : i + 3] for i in range(len(text) - 2)}


@dataclass
class PoolIndex:
    """Pooled subgraphs with namespaced keys, plus adjacency."""

    nodes: dict[str, Node] = field(default_factory=dict)
    slug_of: dict[str, str] = field(default_factory=dict)
    edges: list[tuple[str, str, Edge]] = field(default_factory=list)
    out: dict[str, list[tuple[str, Edge]]] = field(default_factory=lambda: defaultdict(list))
    into: dict[str, list[tuple[str, Edge]]] = field(default_factory=lambda: defaultdict(list))
    papers: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))
    titles: dict[str, str] = field(default_factory=dict)

    @classmethod
    def build(cls, subgraphs: list[Subgraph]) -> PoolIndex:
        index = cls()
        for sg in subgraphs:
            for paper in sg.papers.values():
                index.titles[paper.id] = f"{(paper.authors or ['?'])[0].split()[-1]} {paper.year}"
            for node in sg.nodes.values():
                key = f"{sg.slug}/{node.id}"
                index.nodes[key], index.slug_of[key] = node, sg.slug
                index.papers[key].update(p.paper_id for p in node.provenance)
            for edge in sg.edges:
                s, t = f"{sg.slug}/{edge.source}", f"{sg.slug}/{edge.target}"
                index.edges.append((s, t, edge))
                index.out[s].append((t, edge))
                index.into[t].append((s, edge))
                for key in (s, t):
                    index.papers[key].add(edge.provenance[0].paper_id)
        return index

    def degree(self, key: str) -> int:
        return len(self.out[key]) + len(self.into[key])

    def text(self, key: str) -> str:
        node = self.nodes[key]
        return f"{node.label} {node.description} {_ID_PREFIX.sub('', node.id).replace('-', ' ')}"

    def context(self, key: str, limit: int = 6) -> list[str]:
        """A few incident relationships, so a judge sees how the node is used."""
        lines = [f"-{e.relation}-> {self.nodes[t].label}" for t, e in self.out[key]]
        lines += [f"<-{e.relation}- {self.nodes[s].label}" for s, e in self.into[key]]
        return lines[:limit]


def candidates(
    subgraphs: list[Subgraph],
    judged: list[Alignment] = (),
    budget: int = 40,
    min_plausibility: float = 0.3,
    node_type: str | None = None,
) -> list[dict]:
    """Rank unjudged cross-subgraph node pairs by plausibility x consequence."""
    index = PoolIndex.build(subgraphs)
    done = {(x.a, x.b) for x in judged}
    bags = {key: Counter(_tokens(index.text(key))) for key in index.nodes}
    grams = {key: _trigrams(index.nodes[key].label) for key in index.nodes}
    df = Counter(token for bag in bags.values() for token in bag)
    idf = {token: math.log(1 + len(bags) / count) for token, count in df.items()}
    norm = {k: math.sqrt(sum((c * idf[t]) ** 2 for t, c in bag.items())) or 1.0 for k, bag in bags.items()}
    top_degree = max((index.degree(k) for k in index.nodes), default=1) or 1

    found = []
    for a, b in combinations(sorted(index.nodes), 2):
        na, nb = index.nodes[a], index.nodes[b]
        if index.slug_of[a] == index.slug_of[b] or na.type is not nb.type or (a, b) in done:
            continue
        if node_type and na.type.value != node_type:
            continue
        shared = bags[a].keys() & bags[b].keys()
        cosine = sum(bags[a][t] * bags[b][t] * idf[t] ** 2 for t in shared) / (norm[a] * norm[b])
        union = grams[a] | grams[b]
        trigram = len(grams[a] & grams[b]) / len(union) if union else 0.0
        signals = {"cosine": round(cosine, 3), "label_trigram": round(trigram, 3)}
        plausibility = 0.6 * cosine + 0.4 * trigram
        if na.id == nb.id:
            signals["same_id"] = True
            plausibility = max(plausibility, 0.9)
        if common := index.papers[a] & index.papers[b]:
            signals["shared_papers"] = sorted(common)
            plausibility = min(1.0, plausibility + 0.15)
        if na.type is NodeType.HYPOTHESIS and plausibility < min_plausibility:
            # Hypotheses are few, paraphrased beyond lexical reach, and joining two of them
            # changes the most structure: always worth a judgement, ranked by consequence.
            signals["always_proposed"] = True
            plausibility = min_plausibility
        if plausibility < min_plausibility:
            continue
        # How much structure a match would join: well-connected nodes on both sides matter most.
        consequence = math.log1p(index.degree(a)) * math.log1p(index.degree(b)) / math.log1p(top_degree) ** 2
        found.append(
            {
                "a": a,
                "b": b,
                "type": na.type.value,
                "priority": round(plausibility * (0.5 + 0.5 * consequence), 3),
                "plausibility": round(plausibility, 3),
                "consequence": round(consequence, 3),
                "signals": signals,
                "a_node": _brief(index, a),
                "b_node": _brief(index, b),
            }
        )
    found.sort(key=lambda c: -c["priority"])
    return found[:budget]


def _brief(index: PoolIndex, key: str) -> dict:
    node = index.nodes[key]
    brief = {"label": node.label, "context": index.context(key)}
    if node.description:
        brief["description"] = node.description
    if node.outcome:
        brief["outcome"] = node.outcome.value
    if node.attrs:
        brief["attrs"] = node.attrs
    return brief


def check_alignments(subgraphs: list[Subgraph], alignments: list[Alignment]) -> list[str]:
    index = PoolIndex.build(subgraphs)
    errors = []
    for x in alignments:
        missing = [key for key in (x.a, x.b) if key not in index.nodes]
        if missing:
            errors.append(f"{x.a} ~ {x.b}: not in the pool: {missing}")
        elif index.nodes[x.a].type is not index.nodes[x.b].type and x.verdict is not Verdict.RELATED:
            errors.append(
                f"{x.a} ~ {x.b}: a {index.nodes[x.a].type} and a {index.nodes[x.b].type} "
                f"can only be 'related', not {x.verdict.value!r}"
            )
    return errors


# --- observation -------------------------------------------------------------


class _Clusters:
    """Union-find over 'same' verdicts. Nodes stay separate; this is only a reading of them."""

    def __init__(self, keys: list[str]) -> None:
        self.parent = {key: key for key in keys}
        self.confidence: dict[str, float] = {}

    def find(self, key: str) -> str:
        while self.parent[key] != key:
            self.parent[key] = self.parent[self.parent[key]]
            key = self.parent[key]
        return key

    def union(self, a: str, b: str, confidence: float) -> None:
        ra, rb = self.find(a), self.find(b)
        weakest = min(confidence, self.confidence.get(ra, 1.0), self.confidence.get(rb, 1.0))
        self.parent[rb] = ra
        self.confidence[ra] = weakest


def observe(subgraphs: list[Subgraph], alignments: list[Alignment], min_confidence: float = 0.7) -> dict:
    index = PoolIndex.build(subgraphs)
    clusters = _Clusters(list(index.nodes))
    soft: list[Alignment] = []
    for x in alignments:
        if x.a not in index.nodes or x.b not in index.nodes:
            continue
        if x.verdict is Verdict.SAME and x.confidence >= min_confidence:
            clusters.union(x.a, x.b, x.confidence)
        elif x.verdict in (Verdict.BROADER, Verdict.NARROWER, Verdict.RELATED):
            soft.append(x)

    members: dict[str, list[str]] = defaultdict(list)
    for key in index.nodes:
        members[clusters.find(key)].append(key)
    spanning = {root: keys for root, keys in members.items() if len({index.slug_of[k] for k in keys}) > 1}

    def label(key: str) -> str:
        return index.nodes[key].label

    def conditions_of(key: str) -> set[str]:
        """Condition clusters an experiment or observation sits under, directly or via its experiment."""
        node = index.nodes[key]
        found = {clusters.find(t) for t, e in index.out[key] if e.relation in (Relation.PERFORMED_UNDER, Relation.OBSERVED_UNDER)}
        if node.type is NodeType.OBSERVATION:
            for s, e in index.into[key]:
                if e.relation is Relation.YIELDS:
                    found |= conditions_of(s)
        return found

    def hypotheses_of(key: str) -> set[str]:
        node = index.nodes[key]
        found = {t for t, e in index.out[key] if e.relation in (Relation.TESTS, Relation.SUPPORTS, Relation.CONTRADICTS) and index.nodes[t].type is NodeType.HYPOTHESIS}
        if node.type is NodeType.EXPERIMENT:
            for t, e in index.out[key]:
                if e.relation is Relation.YIELDS:
                    found |= hypotheses_of(t)
        return found

    attached: dict[str, set[str]] = defaultdict(set)
    for key, node in index.nodes.items():
        if node.type in (NodeType.EXPERIMENT, NodeType.OBSERVATION):
            for root in conditions_of(key):
                attached[root].add(key)

    # A condition nearly every experiment sits under says little; a rare one says a lot.
    n_experiments = sum(1 for n in index.nodes.values() if n.type is NodeType.EXPERIMENT) or 1

    def specificity(root: str) -> float:
        used = sum(1 for k in attached[root] if index.nodes[k].type is NodeType.EXPERIMENT)
        return round(1 - used / n_experiments, 2)

    # 1. Bridges: one condition, reached independently from different questions.
    bridges = []
    for root, keys in spanning.items():
        if index.nodes[root].type is not NodeType.CONDITION:
            continue
        sides = defaultdict(list)
        for key in attached[root]:
            if index.nodes[key].type is NodeType.EXPERIMENT:
                sides[index.slug_of[key]].append(key)
        if len(sides) < 2:
            continue
        bridges.append(
            {
                "condition": sorted(label(k) for k in keys),
                "specificity": specificity(root),
                "alignment_confidence": clusters.confidence.get(root, 1.0),
                "experiments": {slug: sorted(label(k) for k in ks) for slug, ks in sides.items()},
                "hypotheses_reached": {
                    slug: sorted({label(h) for k in ks for h in hypotheses_of(k)}) for slug, ks in sides.items()
                },
            }
        )
    bridges.sort(key=lambda b: -math.prod(len(v) for v in b["experiments"].values()))

    # 2. Failures that share a condition across papers.
    shared_failures = []
    for root, keys in attached.items():
        failed = [k for k in keys if index.nodes[k].outcome in FAILURES]
        papers = {p for k in failed for p in index.papers[k]}
        if len(failed) >= 2 and len(papers) >= 2:
            shared_failures.append(
                {
                    "condition": sorted({label(k) for k in members[root]}),
                    "specificity": specificity(root),
                    "observations": sorted(f"[{index.nodes[k].outcome.value}] {label(k)}" for k in failed),
                    "papers": sorted(index.titles.get(p, p) for p in papers),
                    "subgraphs": sorted({index.slug_of[k] for k in failed}),
                }
            )
    shared_failures.sort(key=lambda f: (-len(f["subgraphs"]), -f["specificity"], -len(f["papers"])))

    # 3. Cross-bearing: an experiment from one question run under the conditions another
    #    question's hypothesis is tested under, where no hypothesis link already says so.
    entailed = {(x.a, x.b) for x in alignments if x.verdict in (Verdict.SAME, Verdict.BROADER, Verdict.NARROWER)}
    entailed |= {(b, a) for a, b in entailed}
    tested_under: dict[str, set[str]] = defaultdict(set)
    for key, node in index.nodes.items():
        if node.type is NodeType.EXPERIMENT:
            for h in hypotheses_of(key):
                tested_under[h] |= conditions_of(key)
    cross = []
    for key, node in index.nodes.items():
        if node.type is not NodeType.EXPERIMENT:
            continue
        mine, own = conditions_of(key), hypotheses_of(key)
        for h, theirs in tested_under.items():
            overlap = {r for r in mine & theirs if r in spanning}
            if index.slug_of[h] == index.slug_of[key] or len(overlap) < 2:
                continue
            if any((mine_h, h) in entailed for mine_h in own):
                continue
            results = [t for t, e in index.out[key] if e.relation is Relation.YIELDS]
            cross.append(
                {
                    "experiment": node.label,
                    "from": index.slug_of[key],
                    "results": sorted(f"[{index.nodes[t].outcome.value}] {label(t)}" for t in results),
                    "may_bear_on": label(h),
                    "in": index.slug_of[h],
                    "shared_conditions": sorted(label(r) for r in overlap),
                    "score": round(sum(specificity(r) for r in overlap), 2),
                    "confidence": round(min(clusters.confidence.get(r, 1.0) for r in overlap), 2),
                }
            )
    cross.sort(key=lambda c: -c["score"])

    # 4. Contradictions and the conditions that differ between the two results.
    regimes = []
    for s, t, edge in index.edges:
        if edge.relation is Relation.CONTRADICTS and index.nodes[t].type is NodeType.OBSERVATION:
            cs, ct = conditions_of(s), conditions_of(t)
            regimes.append(
                {
                    "a": label(s),
                    "b": label(t),
                    "confidence": edge.confidence,
                    "shared_conditions": sorted(label(r) for r in cs & ct),
                    "only_a": sorted(label(r) for r in cs - ct),
                    "only_b": sorted(label(r) for r in ct - cs),
                }
            )

    # 5. Hypotheses judged the same or related across questions, with their pooled evidence.
    def evidence(h: str) -> dict:
        tally = {"supports": 0, "contradicts": 0, "papers": set()}
        for s, e in index.into[h]:
            if e.relation in (Relation.SUPPORTS, Relation.CONTRADICTS):
                tally[e.relation.value] += 1
                tally["papers"].add(e.provenance[0].paper_id)
        return {"supports": tally["supports"], "contradicts": tally["contradicts"], "papers": len(tally["papers"])}

    linked = []
    for x in alignments:
        if x.a in index.nodes and index.nodes[x.a].type is NodeType.HYPOTHESIS and x.verdict is not Verdict.DIFFERENT:
            linked.append(
                {
                    "verdict": x.verdict.value,
                    "confidence": x.confidence,
                    "a": {"hypothesis": label(x.a), "in": index.slug_of[x.a], **evidence(x.a)},
                    "b": {"hypothesis": label(x.b), "in": index.slug_of[x.b], **evidence(x.b)},
                    "rationale": x.rationale,
                }
            )

    # 6. Absent experiments. A condition that scopes a result bearing on one hypothesis is a
    #    moderator that matters for it. If a linked hypothesis in another subgraph was never
    #    tested under that condition, the pool is pointing at an experiment nobody ran.
    def moderators(h: str) -> dict[str, list[str]]:
        found: dict[str, list[str]] = defaultdict(list)
        for s_key, e in index.into[h]:
            if e.relation in (Relation.SUPPORTS, Relation.CONTRADICTS):
                for t, scoped in index.out[s_key]:
                    if scoped.relation is Relation.OBSERVED_UNDER:
                        found[clusters.find(t)].append(f"[{index.nodes[s_key].outcome.value}] {label(s_key)}")
        return found

    def ever_under(h: str) -> set[str]:
        keys = [s_key for s_key, e in index.into[h] if e.relation in (Relation.TESTS, Relation.SUPPORTS, Relation.CONTRADICTS)]
        return set().union(*(conditions_of(k) for k in keys)) if keys else set()

    untested = []
    for x in alignments:
        if x.verdict is Verdict.DIFFERENT or x.a not in index.nodes or index.nodes[x.a].type is not NodeType.HYPOTHESIS:
            continue
        for source, target in ((x.a, x.b), (x.b, x.a)):
            missing = {c: obs for c, obs in moderators(source).items() if c not in ever_under(target)}
            if missing:
                untested.append(
                    {
                        "hypothesis": label(target),
                        "in": index.slug_of[target],
                        "never_tested_under": sorted(label(c) for c in missing),
                        "which_scope_results_on": label(source),
                        "from": index.slug_of[source],
                        "those_results": sorted({o for obs in missing.values() for o in obs}),
                        "hypotheses_judged": x.verdict.value,
                        "confidence": x.confidence,
                        "rationale": x.rationale,
                    }
                )
    untested.sort(key=lambda u: (-u["confidence"], -len(u["never_tested_under"])))

    thin = [
        {"hypothesis": label(k), "in": index.slug_of[k], **evidence(k)}
        for k, n in index.nodes.items()
        if n.type is NodeType.HYPOTHESIS and evidence(k)["papers"] < 2
    ]

    return {
        "pool": {"subgraphs": len(subgraphs), "nodes": len(index.nodes), "alignments": len(alignments),
                 "same_clusters_spanning_subgraphs": len(spanning), "soft_links": len(soft)},
        "bridging_conditions": bridges,
        "shared_condition_failures": shared_failures,
        "cross_bearing": cross[:25],
        "contradictions_by_regime": regimes,
        "absent_experiments": untested,
        "linked_hypotheses": linked,
        "thinly_evidenced_hypotheses": thin,
    }


def check_leads(subgraphs: list[Subgraph], alignments: list[Alignment], leads: list[Lead]) -> list[str]:
    """A lead must point at things in the pool, and cannot be surer than its weakest link."""
    index = PoolIndex.build(subgraphs)
    verdicts = {(x.a, x.b): x for x in alignments}
    errors = []
    for lead in leads:
        if missing := [key for key in lead.nodes if key not in index.nodes]:
            errors.append(f"lead {lead.id}: nodes not in the pool: {missing}")
        weakest = 1.0
        for a, b in lead.alignments:
            verdict = verdicts.get((a, b)) or verdicts.get((b, a))
            if verdict is None:
                errors.append(f"lead {lead.id}: no alignment verdict for {a} ~ {b}")
            elif verdict.verdict is Verdict.DIFFERENT:
                errors.append(f"lead {lead.id}: rests on {a} ~ {b}, which was judged different")
            else:
                weakest = min(weakest, verdict.confidence)
        if len({index.slug_of[k] for k in lead.nodes if k in index.nodes}) > 1 and not lead.alignments:
            errors.append(f"lead {lead.id}: spans subgraphs but names no alignment verdict it rests on")
        if lead.confidence > weakest:
            errors.append(
                f"lead {lead.id}: confidence {lead.confidence:.2f} exceeds its weakest alignment ({weakest:.2f})"
            )
    return errors
