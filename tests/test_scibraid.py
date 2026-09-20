import json

import httpx
import pytest

import threading

from scibraid import align, main, openalex, store
from scibraid.cli import _md_table, make_server
from scibraid.lint import lint
from scibraid.models import Alignment, Batch, Lead, Paper, Subgraph
from scibraid.pool import HttpPool, LocalPool

ABSTRACT = (
    "We tested whether compound X reduces tumour growth in mice. Under hypoxic "
    "conditions, treated animals showed no reduction in tumour volume “relative to "
    "controls”. We suggest the effect reported earlier depends on oxygen tension."
)


@pytest.fixture(autouse=True)
def scibraid_home(tmp_path, monkeypatch):
    monkeypatch.setenv("SCIBRAID_HOME", str(tmp_path))
    monkeypatch.delenv("SCIBRAID_POOL_URL", raising=False)
    store.save_paper(Paper(id="W1", title="Compound X under hypoxia", abstract=ABSTRACT))
    store.save_paper(Paper(id="W2", title="No abstract held"))


def prov(passage, paper="W1", location="abstract"):
    return [{"paper_id": paper, "passage": passage, "location": location}]


def batch(**overrides):
    data = {
        "nodes": [
            {"id": "h:x-reduces-growth", "type": "hypothesis", "label": "X reduces tumour growth"},
            {
                "id": "e:mouse-hypoxia",
                "type": "experiment",
                "label": "X in mice under hypoxia",
                "provenance": prov("We tested whether compound X reduces tumour growth in mice."),
            },
            {"id": "c:hypoxia", "type": "condition", "label": "Hypoxic conditions"},
            {
                "id": "o:no-reduction",
                "type": "observation",
                "outcome": "null",
                "label": "No reduction in tumour volume",
            },
        ],
        "edges": [
            {
                "source": "e:mouse-hypoxia",
                "target": "h:x-reduces-growth",
                "relation": "tests",
                "confidence": 0.9,
                "asserted_by": "author",
                "provenance": prov("We tested whether compound X reduces tumour growth"),
            },
            {
                "source": "e:mouse-hypoxia",
                "target": "c:hypoxia",
                "relation": "performed_under",
                "confidence": 0.95,
                "asserted_by": "author",
                "provenance": prov("Under hypoxic conditions"),
            },
            {
                "source": "e:mouse-hypoxia",
                "target": "o:no-reduction",
                "relation": "yields",
                "confidence": 0.9,
                "asserted_by": "author",
                "provenance": prov('showed no reduction ... "relative to   controls"'),
            },
            {
                "source": "o:no-reduction",
                "target": "h:x-reduces-growth",
                "relation": "contradicts",
                "confidence": 0.6,
                "asserted_by": "model",
                "provenance": prov("treated animals showed no reduction in tumour volume"),
            },
        ],
    }
    data.update(overrides)
    return Batch.model_validate(data)


def test_passage_matching_ignores_typography_and_allows_ordered_elision():
    assert store.passage_in_text('no reduction … "relative to controls"', ABSTRACT)
    assert not store.passage_in_text("relative to controls ... no reduction", ABSTRACT)
    assert not store.passage_in_text("showed a marked reduction", ABSTRACT)
    assert not store.passage_in_text("...", ABSTRACT)


def test_passage_cut_off_mid_word_is_rejected():
    assert store.passage_in_text("no reduction in tumour volume", ABSTRACT)
    assert not store.passage_in_text("no reduction in tumour vol", ABSTRACT)
    assert not store.passage_in_text("eated animals showed no reduction", ABSTRACT)
    assert not store.passage_in_text("treated ani ... tumour volume", ABSTRACT)
    # punctuation at either end is a boundary, and a later whole-word match still counts
    assert store.passage_in_text("in mice. Under hypoxic conditions,", ABSTRACT)
    assert store.passage_in_text("on", "reduction depends on oxygen")

    sg = Subgraph(slug="q", question="Does X work?")
    bad = batch()
    bad.edges[0].provenance[0].passage = "We tested whether compound X reduces tumour gro"
    [error] = store.add_batch(sg, bad).errors
    assert "starts or ends mid-word" in error


def test_add_applies_valid_batch_and_marks_passages_verified():
    sg = Subgraph(slug="q", question="Does X work?")
    report = store.add_batch(sg, batch())
    assert report.ok, report.errors
    assert (report.nodes_added, report.edges_added) == (4, 4)
    assert all(p.verified for e in sg.edges for p in e.provenance)
    assert list(sg.papers) == ["W1"]


def test_add_rejects_fabricated_passage_and_applies_nothing():
    sg = Subgraph(slug="q", question="Does X work?")
    bad = batch()
    bad.edges[0].provenance[0].passage = "X abolished tumour growth entirely"
    report = store.add_batch(sg, bad)
    assert not report.ok
    assert "not a verbatim quote" in report.errors[0]
    assert not sg.nodes and not sg.edges


def test_add_rejects_bad_endpoints_unknown_nodes_and_uncached_papers():
    sg = Subgraph(slug="q", question="Does X work?")
    bad = batch()
    bad.edges[0].source, bad.edges[0].target = bad.edges[0].target, bad.edges[0].source
    bad.edges[1].target = "c:missing"
    bad.edges[2].provenance[0].paper_id = "W404"
    errors = "\n".join(store.add_batch(sg, bad).errors)
    assert "tests cannot link hypothesis->experiment" in errors
    assert "unknown node(s) ['c:missing']" in errors
    assert "'W404' is not cached" in errors


def test_passage_without_held_text_is_a_warning_until_text_is_attached():
    sg = Subgraph(slug="q", question="Does X work?")
    extra = Batch.model_validate(
        {
            "nodes": [
                {
                    "id": "c:normoxia",
                    "type": "condition",
                    "label": "Normoxia",
                    "provenance": prov("cells were kept at 21% oxygen", "W2", "methods"),
                }
            ]
        }
    )
    report = store.add_batch(sg, extra)
    assert report.ok and "unverified" in report.warnings[0]
    assert sg.nodes["c:normoxia"].provenance[0].verified is None

    store.attach_text("W2", "Methods. Cells were kept at 21% oxygen throughout.")
    assert store.verify("W2", "cells were kept at 21% oxygen", "methods") is True
    assert store.verify("W2", "cells were kept at 1% oxygen", "methods") is False


def test_same_edge_from_second_paper_accumulates_and_reextraction_replaces():
    sg = Subgraph(slug="q", question="Does X work?")
    store.add_batch(sg, batch())
    store.save_paper(Paper(id="W3", title="Replication", abstract="X did not shrink tumours."))
    again = Batch.model_validate(
        {
            "edges": [
                {
                    "source": "o:no-reduction",
                    "target": "h:x-reduces-growth",
                    "relation": "contradicts",
                    "confidence": 0.7,
                    "asserted_by": "author",
                    "provenance": prov("X did not shrink tumours", "W3"),
                }
            ]
        }
    )
    assert store.add_batch(sg, again).edges_added == 1
    report = store.add_batch(sg, batch())
    assert (report.nodes_merged, report.edges_replaced, report.edges_added) == (4, 4, 0)
    assert len(sg.edges) == 5


def test_observation_requires_outcome():
    with pytest.raises(ValueError, match="needs an outcome"):
        Batch.model_validate({"nodes": [{"id": "o:x", "type": "observation", "label": "x"}]})


def test_lint_flags_gaps():
    sg = Subgraph(slug="q", question="Does X work?")
    store.add_batch(sg, batch(edges=batch().edges[:1]))
    findings = "\n".join(lint(sg))
    assert "e:mouse-hypoxia: experiment yields no observation" in findings
    assert "e:mouse-hypoxia: experiment has no conditions" in findings
    assert "c:hypoxia: orphan condition" in findings
    assert "h:x-reduces-growth: hypothesis has no direct evidence" in findings
    assert "only one paper" in findings


def test_openalex_search_rebuilds_abstract_and_classifies_tier():
    def handler(request):
        assert request.url.params["search"] == "compound x"
        assert "from_publication_date:2020-01-01" in request.url.params["filter"]
        work = {
            "id": "https://openalex.org/W99",
            "doi": "https://doi.org/10.1/abc",
            "title": "A preprint",
            "publication_year": 2021,
            "type": "preprint",
            "cited_by_count": 3,
            "authorships": [{"author": {"display_name": "A. Author"}}],
            "primary_location": {"source": {"display_name": "bioRxiv"}},
            "abstract_inverted_index": {"works": [2], "X": [0, 3], "sometimes": [1]},
        }
        return httpx.Response(200, json={"results": [work]})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    [paper] = openalex.search("compound x", from_year=2020, client=client)
    assert (paper.id, paper.doi, paper.source_tier) == ("W99", "10.1/abc", "grey")
    assert paper.abstract == "X sometimes works X"


def test_local_pool_namespaces_nodes_and_repush_replaces():
    sg = Subgraph(slug="q", question="Does X work?")
    store.add_batch(sg, batch())
    pool = LocalPool()
    pool.push(sg)
    pool.push(sg)
    assert pool.listing() == [
        {"slug": "q", "question": "Does X work?", "pushed": pool.listing()[0]["pushed"],
         "nodes": 4, "edges": 4}
    ]


def test_http_pool_posts_subgraph_json():
    seen = {}

    def handler(request):
        seen["path"], seen["body"] = request.url.path, json.loads(request.content)
        return httpx.Response(201, json={})

    sg = Subgraph(slug="q", question="Does X work?")
    client = httpx.Client(transport=httpx.MockTransport(handler))
    assert HttpPool("https://pool.example/", client).push(sg).endswith("/subgraphs/q")
    assert seen["path"] == "/subgraphs" and seen["body"]["slug"] == "q"


def test_cli_end_to_end(tmp_path, capsys):
    path = tmp_path / "batch.json"
    path.write_text(batch().model_dump_json())
    assert main(["new", "does-x-work", "--question", "Does X work?"]) == 0
    assert main(["new", "does-x-work", "--question", "again"]) == 1
    assert main(["add", "does-x-work", str(path)]) == 0
    assert main(["show", "does-x-work", "--format", "mermaid"]) == 0
    assert main(["lint", "does-x-work"]) == 0
    assert main(["pool", "does-x-work"]) == 0
    assert main(["pool", "--list"]) == 0
    out = capsys.readouterr().out
    assert "graph LR" in out and '"edges": 4' in out

    path.write_text('{"nodes": [{"id": "Bad Id", "type": "gene", "label": ""}]}')
    assert main(["add", "does-x-work", str(path)]) == 1
    assert "nodes.0.type" in capsys.readouterr().out


def test_view_embeds_subgraphs_as_inert_json(tmp_path, capsys):
    sg = Subgraph(slug="q", question="Is </script><script>alert(1)</script> safe?")
    store.add_batch(sg, batch())
    store.save_subgraph(sg)
    out = tmp_path / "view.html"
    assert main(["view", "-o", str(out)]) == 0
    html = out.read_text()
    assert "/*__DATA__*/" not in html and "alert(1)</script>" not in html
    embedded = html.split('type="application/json">', 1)[1].split("</script>", 1)[0]
    [shown] = json.loads(embedded)["subgraphs"]
    assert shown["question"] == sg.question and len(shown["edges"]) == 4


def test_view_server_rerenders_on_each_request():
    server = make_server([], 0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    try:
        assert '"subgraphs": [], "alignments": []' in httpx.get(url).text
        store.save_subgraph(Subgraph(slug="later", question="Added after the server started?"))
        assert "Added after the server started?" in httpx.get(url).text
        assert httpx.get(url + "etc/passwd").status_code == 404
    finally:
        server.shutdown()
        server.server_close()


def second_subgraph():
    """A different question whose experiment shares the hypoxia condition under another name."""
    store.save_paper(Paper(id="W5", title="Drug Y in low oxygen", abstract="Under low oxygen, drug Y failed to slow growth."))
    sg = Subgraph(slug="y", question="Does Y work?")
    report = store.add_batch(sg, Batch.model_validate({
        "nodes": [
            {"id": "h:y-slows-growth", "type": "hypothesis", "label": "Y slows tumour growth"},
            {"id": "e:y-low-oxygen", "type": "experiment", "label": "Y under low oxygen"},
            {"id": "c:low-oxygen", "type": "condition", "label": "Low oxygen (hypoxic) culture conditions"},
            {"id": "c:normoxia", "type": "condition", "label": "Normal oxygen, not hypoxic conditions"},
            {"id": "o:y-no-effect", "type": "observation", "outcome": "negative", "label": "Y failed to slow growth"},
        ],
        "edges": [
            {"source": "e:y-low-oxygen", "target": "h:y-slows-growth", "relation": "tests", "confidence": 0.9,
             "asserted_by": "author", "provenance": prov("drug Y failed to slow growth", "W5")},
            {"source": "e:y-low-oxygen", "target": "c:low-oxygen", "relation": "performed_under", "confidence": 0.9,
             "asserted_by": "author", "provenance": prov("Under low oxygen", "W5")},
            {"source": "e:y-low-oxygen", "target": "o:y-no-effect", "relation": "yields", "confidence": 0.9,
             "asserted_by": "author", "provenance": prov("drug Y failed to slow growth", "W5")},
        ],
    }))
    assert report.ok, report.errors
    return sg


def test_candidates_propose_cross_subgraph_pairs_and_skip_judged_ones():
    first = Subgraph(slug="q", question="Does X work?")
    store.add_batch(first, batch())
    found = align.candidates([first, second_subgraph()])
    pairs = {(c["a"], c["b"]) for c in found}
    assert ("q/c:hypoxia", "y/c:low-oxygen") in pairs
    assert ("q/h:x-reduces-growth", "y/h:y-slows-growth") in pairs  # near-restatements score like any other pair
    assert all(c["a"].split("/")[0] != c["b"].split("/")[0] for c in found)
    assert not any("e:" in c["a"] and "c:" in c["b"] for c in found)  # same type only

    judged = [Alignment(a="q/c:hypoxia", b="y/c:low-oxygen", verdict="same", confidence=0.9, rationale="Both mean hypoxic culture.")]
    assert ("q/c:hypoxia", "y/c:low-oxygen") not in {(c["a"], c["b"]) for c in align.candidates([first, second_subgraph()], judged)}


def test_alignment_verdicts_are_validated_and_canonicalised():
    flipped = Alignment(a="y/c:low-oxygen", b="q/c:hypoxia", verdict="broader", confidence=0.8, rationale="Low oxygen covers more.")
    assert (flipped.a, flipped.verdict) == ("q/c:hypoxia", "narrower")
    with pytest.raises(ValueError, match="same subgraph"):
        Alignment(a="q/c:a", b="q/c:b", verdict="same", confidence=0.9, rationale="Same subgraph, so not allowed.")
    first = Subgraph(slug="q", question="Does X work?")
    store.add_batch(first, batch())
    bad = [Alignment(a="q/c:hypoxia", b="y/e:y-low-oxygen", verdict="same", confidence=0.9, rationale="A condition is not an experiment."),
           Alignment(a="q/c:hypoxia", b="y/c:missing", verdict="same", confidence=0.9, rationale="Points at nothing in the pool.")]
    errors = "\n".join(align.check_alignments([first, second_subgraph()], bad))
    assert "can only be 'related'" in errors and "not in the pool" in errors


def test_observe_finds_failures_sharing_an_aligned_condition():
    first = Subgraph(slug="q", question="Does X work?")
    store.add_batch(first, batch())
    graphs = [first, second_subgraph()]
    assert align.observe(graphs, [])["shared_condition_failures"] == []

    same = Alignment(a="q/c:hypoxia", b="y/c:low-oxygen", verdict="same", confidence=0.9, rationale="Both mean hypoxic culture.")
    report = align.observe(graphs, [same])
    [failure] = report["shared_condition_failures"]
    assert failure["subgraphs"] == ["q", "y"] and len(failure["observations"]) == 2
    [bridge] = report["bridging_conditions"]
    assert set(bridge["experiments"]) == {"q", "y"}
    # a 'same' below the threshold joins nothing
    same.confidence = 0.5
    assert align.observe(graphs, [same])["shared_condition_failures"] == []


def test_alignments_round_trip_through_the_local_pool(capsys):
    first = Subgraph(slug="q", question="Does X work?")
    store.add_batch(first, batch())
    pool = LocalPool()
    pool.push(first)
    pool.push(second_subgraph())
    assert main(["candidates", "--type", "condition", "--lexical"]) == 0
    assert "q/c:hypoxia" in capsys.readouterr().out
    pool.add_alignments([Alignment(a="q/c:hypoxia", b="y/c:low-oxygen", verdict="same", confidence=0.9, rationale="Both mean hypoxic culture.")])
    assert [x.verdict for x in pool.alignments()] == ["same"]
    assert main(["observe"]) == 0 and main(["align", "list"]) == 0
    assert "Both mean hypoxic culture." in capsys.readouterr().out


def lead(**overrides):
    data = {
        "id": "hypoxia-blunts-both", "kind": "shared_failure", "confidence": 0.4,
        "claim": "Hypoxia blunts the effect of both compounds on tumour growth.",
        "nodes": ["q/o:no-reduction", "y/o:y-no-effect"],
        "alignments": [("q/c:hypoxia", "y/c:low-oxygen")],
    }
    data.update(overrides)
    return Lead.model_validate(data)


def test_lead_must_be_checked_before_it_is_judged():
    assert lead().status == "candidate"
    with pytest.raises(ValueError, match="records no checks"):
        lead(status="refuted")
    checks = [{"question": "Is the oxygen level comparable?", "finding": "Yes, both report 1% oxygen.", "sources": ["W1", "W5"]}]
    with pytest.raises(ValueError, match="what would confirm"):
        lead(status="holds", checks=checks)
    with pytest.raises(ValueError, match="say where"):
        lead(status="known", checks=checks)
    assert lead(status="known", checks=checks, known_in=["W9"]).status == "known"


def test_lead_cannot_be_surer_than_its_weakest_alignment():
    first = Subgraph(slug="q", question="Does X work?")
    store.add_batch(first, batch())
    graphs = [first, second_subgraph()]
    same = Alignment(a="q/c:hypoxia", b="y/c:low-oxygen", verdict="same", confidence=0.6, rationale="Both mean hypoxic culture.")
    assert align.check_leads(graphs, [same], [lead()]) == []
    errors = "\n".join(align.check_leads(graphs, [same], [lead(confidence=0.8), lead(id="b", nodes=["q/o:gone", "y/o:y-no-effect"])]))
    assert "exceeds its weakest alignment (0.60)" in errors and "not in the pool" in errors
    assert "no alignment verdict" in align.check_leads(graphs, [], [lead()])[0]
    different = Alignment(a="q/c:hypoxia", b="y/c:low-oxygen", verdict="different", confidence=0.9, rationale="Not the same oxygen level.")
    assert "judged different" in align.check_leads(graphs, [different], [lead()])[0]
    assert "names no alignment" in align.check_leads(graphs, [same], [lead(alignments=[])])[0]


def test_observe_reports_a_linked_hypothesis_never_tested_under_a_moderator():
    first = Subgraph(slug="q", question="Does X work?")
    store.add_batch(first, batch())
    report = store.add_batch(first, Batch.model_validate({
        "nodes": [{"id": "c:high-dose", "type": "condition", "label": "High dose"}],
        "edges": [
            {"source": "o:no-reduction", "target": "c:high-dose", "relation": "observed_under", "confidence": 0.9,
             "asserted_by": "author", "provenance": prov("showed no reduction in tumour volume")},
            {"source": "o:no-reduction", "target": "c:hypoxia", "relation": "observed_under", "confidence": 0.9,
             "asserted_by": "author", "provenance": prov("Under hypoxic conditions")},
        ],
    }))
    assert report.ok, report.errors
    graphs = [first, second_subgraph()]
    verdicts = [
        Alignment(a="q/c:hypoxia", b="y/c:low-oxygen", verdict="same", confidence=0.9, rationale="Both mean hypoxic culture."),
        Alignment(a="q/h:x-reduces-growth", b="y/h:y-slows-growth", verdict="related", confidence=0.6, rationale="Same pathway, different compound."),
    ]
    [gap] = align.observe(graphs, verdicts)["absent_experiments"]
    # Y was tested under hypoxia (aligned), so only the dose is missing
    assert gap["hypothesis"] == "Y slows tumour growth" and gap["never_tested_under"] == ["High dose"]
    assert gap["confidence"] == 0.6
    assert align.observe(graphs, verdicts[:1])["absent_experiments"] == []  # unlinked hypotheses: no claim


def test_leads_round_trip_and_reach_the_viewer(tmp_path, capsys):
    first = Subgraph(slug="q", question="Does X work?")
    store.add_batch(first, batch())
    second = second_subgraph()
    pool = LocalPool()
    for sg in (first, second):
        pool.push(sg)
        store.save_subgraph(sg)
    pool.add_alignments([Alignment(a="q/c:hypoxia", b="y/c:low-oxygen", verdict="same", confidence=0.9, rationale="Both mean hypoxic culture.")])
    path = tmp_path / "leads.json"
    path.write_text(json.dumps([lead(confidence=0.95).model_dump(mode="json")]))
    assert main(["lead", "add", str(path)]) == 1  # surer than its alignment
    path.write_text(json.dumps([lead().model_dump(mode="json")]))
    assert main(["lead", "add", str(path)]) == 0
    assert main(["lead", "list"]) == 0
    assert "Hypoxia blunts the effect" in capsys.readouterr().out
    out = tmp_path / "view.html"
    assert main(["view", "-o", str(out)]) == 0
    assert "hypoxia-blunts-both" in out.read_text()


def test_observe_inherits_conditions_from_narrower_to_broader():
    first = Subgraph(slug="q", question="Does X work?")
    store.add_batch(first, batch())
    graphs = [first, second_subgraph()]
    narrower = Alignment(a="q/c:hypoxia", b="y/c:low-oxygen", verdict="narrower", confidence=0.9, rationale="Hypoxia here is one kind of low oxygen.")
    report = align.observe(graphs, [narrower])
    [failure] = report["shared_condition_failures"]
    assert failure["condition"] == ["Low oxygen (hypoxic) culture conditions"] and failure["subgraphs"] == ["q", "y"]
    # inheritance runs one way only: the broader condition's experiments are not under the narrower one
    broader = Alignment(a="q/c:hypoxia", b="y/c:low-oxygen", verdict="broader", confidence=0.9, rationale="Here hypoxia is the wider category.")
    [failure] = align.observe(graphs, [broader])["shared_condition_failures"]
    assert failure["condition"] == ["Hypoxic conditions"]
    assert align.observe(graphs, [narrower.model_copy(update={"confidence": 0.5})])["shared_condition_failures"] == []


class FakeEmbedder:
    """Puts any text mentioning oxygen or hypoxia on one axis, everything else on another."""

    def vectors(self, texts):
        return [[1.0, 0.0] if ("oxygen" in t.lower() or "hypox" in t.lower()) else [0.0, 1.0] for t in texts]


def test_embeddings_surface_a_match_that_shares_no_words():
    first = Subgraph(slug="q", question="Does X work?")
    store.add_batch(first, batch())
    second = second_subgraph()
    second.nodes["c:low-oxygen"].label = "Cultures starved of O2"  # no words in common with "Hypoxic conditions"
    second.nodes["c:low-oxygen"].description = "oxygen held at one percent"
    pair = ("q/c:hypoxia", "y/c:low-oxygen")
    lexical = {(c["a"], c["b"]) for c in align.candidates([first, second], node_type="condition")}
    assert pair not in lexical
    [found] = [c for c in align.candidates([first, second], node_type="condition", embedder=FakeEmbedder()) if (c["a"], c["b"]) == pair]
    assert found["signals"]["semantic"] == 1.0 and found["plausibility"] >= 0.5
    # the two signals are averaged, so embedding similarity alone cannot reach the top score
    assert found["plausibility"] < 0.9


def test_hypothesis_lists_put_bridged_unjudged_subgraph_pairs_first():
    first = Subgraph(slug="q", question="Does X work?")
    store.add_batch(first, batch())
    third = Subgraph(slug="z", question="An unrelated question?")
    store.add_batch(third, Batch.model_validate({"nodes": [{"id": "h:other", "type": "hypothesis", "label": "Something else entirely"}]}))
    graphs = [first, second_subgraph(), third]
    bridge = Alignment(a="q/c:hypoxia", b="y/c:low-oxygen", verdict="same", confidence=0.9, rationale="Both mean hypoxic culture.")
    lists = align.hypothesis_lists(graphs, [bridge])
    assert lists[0]["subgraphs"] == ["q", "y"] and lists[0]["condition_bridges"] == 1
    [h] = lists[0]["hypotheses"]["q"]
    assert (h["key"], h["supports"], h["contradicts"]) == ("q/h:x-reduces-growth", 0, 1)
    judged = Alignment(a="q/h:x-reduces-growth", b="y/h:y-slows-growth", verdict="related", confidence=0.6, rationale="Same pathway, different compound.")
    lists = align.hypothesis_lists(graphs, [bridge, judged])
    assert lists[-1]["subgraphs"] == ["q", "y"] and len(lists[-1]["already_judged"]) == 1  # reviewed pairs sink


def test_embedding_vectors_are_cached_and_normalised(monkeypatch):
    from scibraid import embed

    calls = []

    class Model:
        def embed(self, texts):
            calls.append(list(texts))
            return [[3.0, 4.0] for _ in texts]

    embedder = embed.FastEmbedder.__new__(embed.FastEmbedder)
    embedder._model = Model()
    import sqlite3
    embedder._db = sqlite3.connect(store.home() / "embeddings.sqlite")
    embedder._db.execute("CREATE TABLE IF NOT EXISTS vectors (key TEXT PRIMARY KEY, dim INTEGER, data BLOB)")
    [v, w] = embedder.vectors(["a", "a"])
    assert v == w and abs(v[0] - 0.6) < 1e-6 and abs(v[1] - 0.8) < 1e-6
    embedder.vectors(["a", "b"])
    assert calls == [["a"], ["b"]]  # "a" came from the cache the second time
    assert embed.calibrated(0.65) == 0.0 and embed.calibrated(0.95) == 1.0


CHECKS = [{"question": "Is the oxygen level comparable?", "finding": "Yes, both report 1% oxygen.", "sources": ["W1", "W5"]}]


def pooled_pair():
    first = Subgraph(slug="q", question="Does X work?")
    store.add_batch(first, batch())
    second = second_subgraph()
    pool = LocalPool()
    for sg in (first, second):
        pool.push(sg)
        store.save_subgraph(sg)
    pool.add_alignments([
        Alignment(a="q/c:hypoxia", b="y/c:low-oxygen", verdict="same", confidence=0.9, rationale="Both mean hypoxic culture."),
        Alignment(a="q/h:x-reduces-growth", b="y/h:y-slows-growth", verdict="related", confidence=0.6, rationale="Same pathway, different compound."),
    ])
    return pool


def test_follow_up_runs_from_lead_to_pooled_subgraph_with_a_derived_hypothesis(capsys):
    pool = pooled_pair()
    held = lead(status="holds", checks=CHECKS, would_confirm="A dose-response under hypoxia.", would_refute="An effect at 1% oxygen.",
                nodes=["q/o:no-reduction", "y/o:y-no-effect", "q/h:x-reduces-growth", "y/h:y-slows-growth"],
                follow_up="Does hypoxia blunt anti-tumour compounds in general?")
    pool.add_leads([held, lead(id="no-question")])
    status = lambda: {f["lead"]: f["status"] for f in align.follow_ups(pool.leads(), pool.subgraphs(), store.list_subgraphs())}
    assert status() == {"hypoxia-blunts-both": "pending"}  # a lead without a follow-up is not listed

    assert main(["new", "hypoxia-general", "--lead", "hypoxia-blunts-both"]) == 0
    sg = store.load_subgraph("hypoxia-general")
    assert sg.prompted_by == "hypoxia-blunts-both" and sg.question == held.follow_up
    [seed] = sg.nodes.values()
    assert seed.type == "hypothesis" and seed.derived_from.lead == held.id and seed.provenance == []
    assert "not yet tested" in "\n".join(lint(sg))
    assert status() == {"hypoxia-blunts-both": "in_progress"}

    # give it one paper-backed link so it is worth pooling
    store.add_batch(sg, Batch.model_validate({
        "nodes": [{"id": "o:review", "type": "observation", "outcome": "positive", "label": "Hypoxia blunts Y"}],
        "edges": [{"source": "o:review", "target": seed.id, "relation": "supports", "confidence": 0.6, "asserted_by": "model",
                   "provenance": prov("Under low oxygen, drug Y failed to slow growth.", "W5")}]}))
    store.save_subgraph(sg)
    assert "not yet tested" not in "\n".join(lint(sg))
    assert main(["pool", "hypoxia-general"]) == 0
    assert "linked 2 derived" in capsys.readouterr().out
    assert status() == {"hypoxia-blunts-both": "reviewed"}
    # Only now can the question be called open: the literature was reviewed and did not settle it.
    unasked = Lead.model_validate({**held.model_dump(), "status": "open"})
    assert "whether the literature already poses the question" in align.check_leads(pool.subgraphs(), pool.alignments(), [unasked])[0]
    opened = Lead.model_validate({**held.model_dump(), "status": "open", "posed_in": []})
    assert align.check_leads(pool.subgraphs(), pool.alignments(), [opened]) == []
    pool.add_leads([opened])
    [question] = align.agenda(pool.leads(), pool.subgraphs())
    assert question["experiment_needed"] == "A dose-response under hypoxia." and question["literature_reviewed_in"] == ["hypoxia-general"]
    assert main(["agenda"]) == 0 and "1 open research question" in capsys.readouterr().out
    derived = [x for x in pool.alignments() if x.a.startswith("hypoxia-general/")]
    assert {x.b for x in derived} == {"q/h:x-reduces-growth", "y/h:y-slows-growth"}
    assert all(x.verdict == "related" and x.confidence == held.confidence for x in derived)


def test_new_from_lead_refuses_unknown_and_refuted_leads():
    pool = pooled_pair()
    pool.add_leads([lead(id="dead", status="refuted", checks=CHECKS, follow_up="Anything?")])
    assert main(["new", "a", "--lead", "missing"]) == 1
    assert main(["new", "a", "--lead", "dead"]) == 1
    assert main(["new", "a"]) == 2


def test_only_a_hypothesis_can_be_derived():
    with pytest.raises(ValueError, match="only a hypothesis"):
        Batch.model_validate({"nodes": [{"id": "c:x", "type": "condition", "label": "x", "derived_from": {"lead": "l", "confidence": 0.5}}]})


def test_observe_marks_and_hides_what_a_lead_already_covers(capsys):
    pool = pooled_pair()
    report = align.observe(pool.subgraphs(), pool.alignments())
    assert report["shared_condition_failures"] and "leads" not in report["shared_condition_failures"][0]
    # a lead that names the failed results but not the condition does not cover the candidate
    pool.add_leads([lead(id="partial")])
    assert "leads" not in align.mark_covered(align.observe(pool.subgraphs(), pool.alignments()), pool.leads())["shared_condition_failures"][0]
    pool.add_leads([lead(status="refuted", checks=CHECKS, nodes=["q/o:no-reduction", "y/o:y-no-effect", "q/c:hypoxia"])])
    marked = align.mark_covered(align.observe(pool.subgraphs(), pool.alignments()), pool.leads())
    assert marked["shared_condition_failures"][0]["leads"] == [{"id": "hypoxia-blunts-both", "status": "refuted"}]
    fresh = align.mark_covered(align.observe(pool.subgraphs(), pool.alignments()), pool.leads(), only_new=True)
    assert fresh["shared_condition_failures"] == []
    assert main(["observe", "--new"]) == 0 and "hypoxia-blunts-both" not in capsys.readouterr().out


def test_repairs_are_listed_until_resolved(capsys):
    pool = pooled_pair()
    pool.add_leads([lead(repairs=[{"kind": "extraction", "target": "q", "problem": "The oxygen level was never recorded as a condition."}])])
    stray = lead(id="stray", repairs=[{"kind": "alignment", "target": "q/c:hypoxia ~ y/c:nowhere", "problem": "Points at a verdict that was never made."}])
    assert "is not a pooled subgraph, node or verdict" in align.check_leads(pool.subgraphs(), pool.alignments(), [stray])[0]
    fine = lead(id="fine", repairs=[{"kind": "alignment", "target": "y/c:low-oxygen ~ q/c:hypoxia", "problem": "Oxygen levels differ tenfold."}])
    assert align.check_leads(pool.subgraphs(), pool.alignments(), [fine]) == []
    [repair] = align.open_repairs(pool.leads())
    assert (repair["lead"], repair["index"], repair["target"]) == ("hypoxia-blunts-both", 0, "q")
    assert main(["repair", "resolve", "hypoxia-blunts-both", "3", "--note", "n/a"]) == 1
    assert main(["repair", "resolve", "hypoxia-blunts-both", "0", "--note", "Added c:one-percent-oxygen to both experiments."]) == 0
    assert align.open_repairs(pool.leads()) == []
    assert pool.leads()[0].repairs[0].resolution.startswith("Added")
    with pytest.raises(ValueError):
        lead(repairs=[{"kind": "typo", "target": "q", "problem": "Not a kind of repair we know."}])


def test_a_question_is_not_open_until_the_literature_has_been_reviewed():
    pool = pooled_pair()
    settle = {"would_confirm": "A dose-response under hypoxia.", "would_refute": "An effect at 1% oxygen."}
    unreviewed = lead(status="open", checks=CHECKS, posed_in=[], follow_up="Does hypoxia blunt anti-tumour compounds in general?", **settle)
    assert "has not been reviewed" in align.check_leads(pool.subgraphs(), pool.alignments(), [unreviewed])[0]
    # with no literature question to ask, a checked lead can go straight to open
    experiment_only = lead(status="open", checks=CHECKS, posed_in=[], **settle)
    assert align.check_leads(pool.subgraphs(), pool.alignments(), [experiment_only]) == []
    with pytest.raises(ValueError, match="what would confirm"):
        lead(status="open", checks=CHECKS)
    assert align.agenda([lead()], pool.subgraphs()) == []  # a candidate is not an open question


def test_agenda_separates_questions_the_literature_already_poses(capsys):
    pool = pooled_pair()
    settle = {"status": "open", "checks": CHECKS, "would_confirm": "A dose-response under hypoxia.", "would_refute": "An effect at 1% oxygen."}
    pool.add_leads([lead(id="asked", confidence=0.6, posed_in=["Smith 2020 calls this an unverified hypothesis"], **settle),
                    lead(id="unasked", confidence=0.3, posed_in=[], **settle)])
    agenda = align.agenda(pool.leads(), pool.subgraphs())
    assert [q["lead"] for q in agenda] == ["unasked", "asked"]  # not-yet-asked first, despite lower confidence
    assert main(["agenda"]) == 0
    out = capsys.readouterr().out
    assert "not found posed anywhere" in out and "already asked in:  Smith 2020" in out
    # leads recorded before the field existed still load, and show as not checked
    assert lead().posed_in is None


def test_markdown_table_escapes_pipes_and_breaks_lists():
    table = _md_table(["a", "b"], [["x | y", ["one", "two"]], [None, 0.5], ["multi\nline", 3]])
    assert table.splitlines() == ["| a | b |", "| --- | --- |", "| x \\| y | one<br>two |", "|  | 0.50 |", "| multi line | 3 |"]


def test_every_listing_command_has_a_markdown_format(capsys):
    pool = pooled_pair()
    held = lead(status="holds", checks=CHECKS, would_confirm="A dose-response under hypoxia.", would_refute="An effect at 1% oxygen.",
                nodes=["q/o:no-reduction", "y/o:y-no-effect", "q/c:hypoxia"], follow_up="Does hypoxia blunt compounds in general?",
                repairs=[{"kind": "extraction", "target": "q", "problem": "The oxygen level was never recorded as a condition."}])
    pool.add_leads([held, lead(id="needs-experiment", status="open", checks=CHECKS, posed_in=[], would_confirm="Run it.", would_refute="It fails.")])
    commands = [["list"], ["pool", "--list"], ["show", "q"], ["candidates", "--lexical"], ["align", "list"], ["lead", "list"],
                ["followups"], ["agenda"], ["repair", "list"], ["observe"]]
    for command in commands:
        assert main([*command, "--format", "markdown"]) == 0, command
        out = capsys.readouterr().out
        assert "| --- |" in out, command
        # every table row has the same number of cells as its header
        table = [line for line in out.splitlines() if line.startswith("|")]
        widths = {line.replace("\\|", "").count("|") for line in table[:3]}
        assert len(widths) == 1, (command, table[:3])
    assert main(["agenda", "--format", "markdown"]) == 0
    out = capsys.readouterr().out
    assert "not found posed anywhere" in out and "Run it." in out
    assert main(["observe", "--format", "markdown"]) == 0
    out = capsys.readouterr().out
    assert "### Failures sharing a condition (1)" in out and "`hypoxia-blunts-both` (holds)" in out


def test_cross_bearing_skips_a_study_already_extracted_into_the_other_subgraph():
    first = Subgraph(slug="q", question="Does X work?")
    store.add_batch(first, batch())
    # a condition only counts as a bridge worth reporting if few experiments share it
    filler = [{"id": f"e:unrelated-{i}", "type": "experiment", "label": f"Unrelated study {i}"} for i in range(14)]
    assert store.add_batch(first, Batch.model_validate({"nodes": filler})).ok
    second = second_subgraph()
    conditions = Alignment(a="q/c:hypoxia", b="y/c:low-oxygen", verdict="same", confidence=0.9, rationale="Both mean hypoxic culture.")
    found = align.observe([first, second], [conditions])["cross_bearing"]
    assert any(c["from"] == "q" and c["may_bear_on"] == "Y slows tumour growth" for c in found)
    # once the two experiments are judged to be one study, q's copy bearing on y's hypothesis is not news
    study = Alignment(a="q/e:mouse-hypoxia", b="y/e:y-low-oxygen", verdict="same", confidence=0.9, rationale="Treat as the same study for the test.")
    found = align.observe([first, second], [conditions, study])["cross_bearing"]
    assert not any(c["from"] == "q" and c["may_bear_on"] == "Y slows tumour growth" for c in found)
