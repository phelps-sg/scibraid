import json

import httpx
import pytest

import threading

from scibraid import align, bibtex, fulltext, identity, main, openalex, store
from scibraid.cli import _md_table, make_server
from scibraid.lint import lint
from scibraid.models import Alignment, AssertedBy, Batch, Builder, Lead, Paper, Subgraph
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
    monkeypatch.delenv("OPENALEX_API_KEY", raising=False)
    monkeypatch.setattr(openalex, "ARXIV_PAUSE", 0)
    monkeypatch.setenv("OPENALEX_API_KEY_FILE", str(tmp_path / "no-key"))
    for name, value in {"SCIBRAID_PERSON": "Ada", "SCIBRAID_AGENT": "test-harness", "SCIBRAID_MODEL": "model-a", "SCIBRAID_SESSION": "s1"}.items():
        monkeypatch.setenv(name, value)
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


WORK = {
    "id": "https://openalex.org/W99",
    "doi": "https://doi.org/10.1/abc",
    "title": "A preprint",
    "publication_year": 2021,
    "type": "preprint",
    "cited_by_count": 3,
    "authorships": [{"author": {"display_name": "A. Author"}}],
    "primary_location": {"source": {"display_name": "bioRxiv"}},
    "abstract_inverted_index": {"works": [2], "X": [0, 3], "sometimes": [1]},
    "open_access": {"is_oa": True, "oa_status": "green", "oa_url": "https://arxiv.org/abs/2101.00001"},
    "best_oa_location": {"pdf_url": "https://arxiv.org/pdf/2101.00001", "landing_page_url": "https://arxiv.org/abs/2101.00001"},
    "locations": [{"landing_page_url": "https://arxiv.org/abs/2101.00001v2"}],
    "ids": {"pmcid": "https://www.ncbi.nlm.nih.gov/pmc/articles/PMC123"},
}


def test_openalex_search_rebuilds_abstract_and_classifies_tier():
    def handler(request):
        # Title and abstract only: OpenAlex's own search also matches full text, which favours open papers.
        assert "search" not in request.url.params
        assert "title_and_abstract.search:compound x  hypoxia" in request.url.params["filter"]
        assert "from_publication_date:2020-01-01" in request.url.params["filter"]
        assert "has_abstract" not in request.url.params["filter"]
        return httpx.Response(200, json={"results": [WORK]})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    [paper] = openalex.search("compound x, hypoxia", from_year=2020, client=client)
    assert (paper.id, paper.doi, paper.source_tier) == ("W99", "10.1/abc", "grey")
    assert paper.abstract == "X sometimes works X"
    assert (paper.oa_status, paper.arxiv_id, paper.pmcid) == ("green", "2101.00001", "PMC123")
    assert paper.oa_url == "https://arxiv.org/pdf/2101.00001"


def test_openalex_searches_a_papers_references_and_the_works_citing_it():
    seen = []

    def handler(request):
        seen.append(dict(request.url.params))
        return httpx.Response(200, json={"results": []})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    openalex.search(citing="W1", client=client)
    openalex.search("bandits", references_of="W2", match="anywhere", client=client)
    assert seen[0]["filter"] == "cites:W1" and seen[0]["sort"] == "cited_by_count:desc"
    assert seen[1]["filter"] == "cited_by:W2" and seen[1]["search"] == "bandits"
    with pytest.raises(openalex.OpenAlexError):
        openalex.search(client=client)


def test_a_paper_is_fetched_by_whatever_identifier_is_to_hand():
    for given, path in {
        "W4318719086": "W4318719086",
        "https://openalex.org/W42": "W42",
        "arxiv:2201.11903": "doi:10.48550/arXiv.2201.11903",
        "https://arxiv.org/pdf/2410.01748v3": "doi:10.48550/arXiv.2410.01748",
        "2502.12143": "doi:10.48550/arXiv.2502.12143",
        "https://doi.org/10.1038/s41586-023-06924-6.": "doi:10.1038/s41586-023-06924-6",
        "pmid: 12345": "pmid:12345",
        "PMC123": "pmcid:PMC123",
    }.items():
        assert openalex.work_path(given) == path
    with pytest.raises(openalex.OpenAlexError):
        openalex.work_path("Wei et al. 2022")

    def handler(request):
        if request.url.path.endswith("doi:10.1234/abc"):
            return httpx.Response(200, json=WORK)
        return httpx.Response(429 if "spent" in request.url.path else 404, json={})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    assert openalex.get("10.1234/abc", client)[0].id == "W99"
    with pytest.raises(openalex.OpenAlexError, match="no such work"):
        openalex.get("10.1234/missing", client)
    with pytest.raises(openalex.OpenAlexError, match="OPENALEX_API_KEY"):
        openalex.get("10.1234/spent", client)


def test_local_pool_namespaces_nodes_and_repush_replaces():
    sg = Subgraph(slug="q", question="Does X work?")
    store.add_batch(sg, batch())
    pool = LocalPool()
    pool.push(sg)
    pool.push(sg)
    assert pool.listing() == [
        {"slug": "q", "question": "Does X work?", "pushed": pool.listing()[0]["pushed"],
         "nodes": 4, "edges": 4, "builders": []}
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


def test_concurrent_adds_to_one_subgraph_all_land(tmp_path):
    assert main(["new", "shared", "--question", "Shared?"]) == 0
    paths = []
    for i in range(12):
        path = tmp_path / f"b{i}.json"
        path.write_text(json.dumps({"nodes": [{"id": f"c:n{i}", "type": "condition", "label": f"Condition {i}"}]}))
        paths.append(path)
    threads = [threading.Thread(target=main, args=(["add", "shared", str(p)],)) for p in paths]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(store.load_subgraph("shared").nodes) == 12  # no lost updates, and the file is still valid JSON
    assert not list((store.home() / "subgraphs").glob("*.tmp"))


def test_a_stale_copy_of_a_lead_is_refused(tmp_path, capsys):
    pool = pooled_pair()
    path = tmp_path / "lead.json"
    path.write_text(json.dumps([lead().model_dump(mode="json", exclude={"updated"})]))
    assert main(["lead", "add", str(path)]) == 0  # new lead: nothing to be stale against
    first = pool.leads()[0].model_dump(mode="json")
    second = dict(first)
    first["confidence"] = 0.5
    path.write_text(json.dumps([first]))
    assert main(["lead", "add", str(path)]) == 0
    second["confidence"] = 0.2  # a second session, still holding the copy it read earlier
    path.write_text(json.dumps([second]))
    capsys.readouterr()
    assert main(["lead", "add", str(path)]) == 1
    assert "changed by someone else since you read it" in capsys.readouterr().out
    assert pool.leads()[0].confidence == 0.5
    # a copy with no `updated` at all is refused too, rather than treated as new
    second.pop("updated")
    path.write_text(json.dumps([second]))
    assert main(["lead", "add", str(path)]) == 1


ADA = Builder(person="Ada", agent="test-harness", model="model-a", session="s1")


def test_who_did_the_work_is_recorded_on_subgraphs_verdicts_and_leads(tmp_path, monkeypatch, capsys):
    assert main(["new", "mine", "--question", "Mine?"]) == 0
    assert store.load_subgraph("mine").builders == [ADA]
    # a second session by someone else, with another model, adds to it
    monkeypatch.setenv("SCIBRAID_PERSON", "Grace")
    path = tmp_path / "b.json"
    path.write_text(batch().model_dump_json())
    assert main(["add", "mine", str(path), "--model", "model-b"]) == 0
    assert main(["add", "mine", str(path), "--model", "model-b"]) == 0  # the same builder is not listed twice
    builders = store.load_subgraph("mine").builders
    assert [(b.person, b.model) for b in builders] == [("Ada", "model-a"), ("Grace", "model-b")]

    pool = pooled_pair()
    verdicts = tmp_path / "v.json"
    verdicts.write_text(json.dumps([{"a": "q/o:no-reduction", "b": "y/o:y-no-effect", "verdict": "related", "confidence": 0.5, "rationale": "Both are failures under low oxygen."}]))
    assert main(["align", "add", str(verdicts)]) == 0
    judged = [x for x in pool.alignments() if x.a == "q/o:no-reduction"][0]
    assert (judged.judge.person, judged.judge.model) == ("Grace", "model-a")
    assert pool.alignments()[0].judge is None  # verdicts recorded without a judge still load

    leads = tmp_path / "l.json"
    leads.write_text(json.dumps([lead().model_dump(mode="json", exclude={"updated"})]))
    assert main(["lead", "add", str(leads)]) == 0
    assert pool.leads()[0].by.person == "Grace"
    assert main(["builder", "add", "mine", "--person", "Alan", "--model", "model-c"]) == 0
    assert len(store.load_subgraph("mine").builders) == 3
    capsys.readouterr()
    assert main(["list"]) == 0
    assert "Ada / model-a / test-harness; Grace / model-b / test-harness; Alan / model-c" in capsys.readouterr().out


def test_independence_has_to_be_shown():
    grace_b = Builder(person="Grace", model="model-b")
    assert identity.relation([ADA], [grace_b]) == "different model"
    assert identity.relation([ADA], [Builder(person="Ada", model="model-a", session="another-session")]) == "same reader"
    assert identity.relation([ADA], [Builder(person="Grace", model="model-a")]) == "same model"  # shared blind spots
    assert identity.relation([ADA], [Builder(person="Ada", model="model-b")]) == "different model"
    assert identity.relation([ADA], [Builder(person="Grace")]) == "unknown"  # no model recorded
    assert identity.relation([ADA], []) == "unknown"
    assert identity.relation([ADA, grace_b], [grace_b]) == "same reader"  # the weakest pair decides


def test_observe_and_agenda_report_how_independent_the_readers_were():
    first = Subgraph(slug="q", question="Does X work?", builders=[ADA])
    store.add_batch(first, batch())
    second = second_subgraph()
    same = Alignment(a="q/c:hypoxia", b="y/c:low-oxygen", verdict="same", confidence=0.9, rationale="Both mean hypoxic culture.")
    readers = lambda: align.observe([first, second], [same])["shared_condition_failures"][0]["readers"]
    assert readers() == "unknown"  # y's builder was never recorded
    second.builders = [Builder(person="Grace", model="model-b")]
    assert readers() == "different model"
    second.builders = [Builder(person="Grace", model="model-a")]
    assert readers() == "same model"
    second.builders = [ADA]
    assert readers() == "same reader"

    settle = {"status": "open", "checks": CHECKS, "posed_in": [], "would_confirm": "Run it.", "would_refute": "It fails."}
    own = lead(id="own", by=ADA.model_dump(), **settle)
    other = lead(id="other", by={"person": "Grace", "model": "model-b"}, **settle)
    agenda = {q["lead"]: q for q in align.agenda([own, other], [first, second])}
    assert agenda["own"]["checker_vs_builders"] == "same reader" and agenda["other"]["checker_vs_builders"] == "different model"


def test_bibtex_entries_and_unique_keys(tmp_path, capsys):
    preprint = Paper(id="arxiv:2301.00001", title="On the Naming of Things", year=2023, authors=["Ada Lovelace", "Grace Hopper"], url="http://arxiv.org/abs/2301.00001")
    article = Paper(id="W7", title="A Study of 50% Gains & Losses", year=2023, authors=["Ada Lovelace"], venue="Journal of Results", doi="10.1/xyz")
    twin = Paper(id="W8", title="Naming Revisited", year=2023, authors=["Ada Lovelace"], venue="Journal of Results")
    many = Paper(id="W9", title="Big Team", year=2020, authors=[f"Author {i}" for i in range(8)])
    bib = bibtex.bibliography([preprint, article, twin, many])
    assert bib["arxiv:2301.00001"]["entry"].startswith("@misc{lovelace2023naminga,") and "eprint = {2301.00001}" in bib["arxiv:2301.00001"]["entry"]
    assert bib["W7"]["entry"].startswith("@article{lovelace2023study,") and "50\\% Gains \\& Losses" in bib["W7"]["entry"] and "doi = {10.1/xyz}" in bib["W7"]["entry"]
    assert bib["W8"]["key"] == "lovelace2023naming"  # first by title keeps the plain key; the second gets a suffix
    assert "and others" in bib["W9"]["entry"]  # author lists are stored cut at eight

    first = Subgraph(slug="q", question="Does X work?")
    store.add_batch(first, batch())
    store.save_subgraph(first)
    out = tmp_path / "refs.bib"
    assert main(["bibtex", "q", "-o", str(out)]) == 0
    assert out.read_text().startswith("@misc{anon") and "Compound X under hypoxia" in out.read_text()
    view = tmp_path / "v.html"
    assert main(["view", "-o", str(view)]) == 0
    assert '"bibtex": {"W1": {"key":' in view.read_text()


def test_two_ids_for_one_condition_are_found_and_merged(tmp_path, capsys):
    sg = Subgraph(slug="q", question="Does X work?")
    store.add_batch(sg, Batch.model_validate(batch()))
    twin = {
        "nodes": [{"id": "c:hypoxic", "type": "condition", "label": "Hypoxic conditions (low oxygen)", "attrs": {"o2_percent": 1}}],
        "edges": [
            {"source": "e:mouse-hypoxia", "target": "c:hypoxic", "relation": "performed_under", "confidence": 0.8,
             "asserted_by": "author", "provenance": prov("depends on oxygen tension")},
            {"source": "o:no-reduction", "target": "c:hypoxic", "relation": "observed_under", "confidence": 0.7,
             "asserted_by": "author", "provenance": prov("Under hypoxic conditions")},
        ],
    }
    assert store.add_batch(sg, Batch.model_validate(twin)).ok
    store.save_subgraph(sg)

    assert [(d["a"], d["b"]) for d in align.duplicates(sg)] == [("c:hypoxia", "c:hypoxic")]

    assert main(["merge", "q", "c:hypoxia", "o:no-reduction"]) == 1  # different types
    capsys.readouterr()
    assert main(["merge", "q", "c:hypoxia", "c:hypoxic"]) == 0
    assert json.loads(capsys.readouterr().out)["dropped"] == "c:hypoxic"

    sg = store.load_subgraph("q")
    assert "c:hypoxic" not in sg.nodes
    kept = sg.nodes["c:hypoxia"]
    assert kept.attrs == {"o2_percent": 1, "merged_from": ["c:hypoxic"]}
    # The two performed_under edges came from one paper, so they are one edge with both passages.
    under = [e for e in sg.edges if e.key == ("e:mouse-hypoxia", "performed_under", "c:hypoxia")]
    assert len(under) == 1 and under[0].confidence == 0.95
    assert {p.passage for p in under[0].provenance} == {"Under hypoxic conditions", "depends on oxygen tension"}
    assert any(e.key == ("o:no-reduction", "observed_under", "c:hypoxia") for e in sg.edges)
    assert not lint(sg) or all("c:hypoxic" not in f for f in lint(sg))


def test_merging_a_pooled_node_reports_what_now_dangles(capsys):
    a, b = Subgraph(slug="a", question="?"), Subgraph(slug="b", question="?")
    for sg in (a, b):
        store.add_batch(sg, Batch.model_validate(batch()))
    extra = {"nodes": [{"id": "c:low-oxygen", "type": "condition", "label": "Low oxygen"}],
             "edges": [{"source": "e:mouse-hypoxia", "target": "c:low-oxygen", "relation": "performed_under",
                        "confidence": 0.8, "asserted_by": "author", "provenance": prov("Under hypoxic conditions")}]}
    store.add_batch(a, Batch.model_validate(extra))
    pool = LocalPool()
    for sg in (a, b):
        store.save_subgraph(sg)
        pool.push(sg)
    pool.add_alignments([Alignment(a="a/c:low-oxygen", b="b/c:hypoxia", verdict="same", confidence=0.9, rationale="Both are low oxygen.")])

    assert main(["merge", "a", "c:hypoxia", "c:low-oxygen"]) == 0
    assert json.loads(capsys.readouterr().out)["now_dangling_in_pool"] == ["verdict a/c:low-oxygen ~ b/c:hypoxia"]


def test_an_inference_cannot_be_recorded_as_near_certain():
    data = batch()
    data.edges[1].asserted_by, data.edges[1].confidence = AssertedBy.MODEL, 0.9
    report = store.add_batch(Subgraph(slug="q", question="?"), Batch.model_validate(data.model_dump()))
    assert not report.ok and "author-asserted" in report.errors[0]
    data.edges[1].confidence = 0.85
    assert store.add_batch(Subgraph(slug="q", question="?"), Batch.model_validate(data.model_dump())).ok


BODY = "<p>" + "We ran the assay under hypoxia and saw nothing. " * 200 + "</p>"


def test_html_keeps_headings_and_mathematics_and_drops_the_page_furniture():
    html = (
        "<nav>Skip to main</nav><h2>3 Methods</h2><p>covers <math alttext='40\\%'><mn>40</mn><annotation>x</annotation></math>"
        " of arms at <math alttext='\\sigma=1.0'><mi>s</mi></math>.</p><script>var a;</script>"
    )
    assert fulltext.html_to_text(html) == "## 3 Methods\n\ncovers 40% of arms at \\sigma=1.0 ."


def test_article_xml_becomes_headed_text_without_the_reference_list():
    xml = (
        "<article><front><abstract><p>Short <italic>summary</italic>.</p></abstract></front><body><sec><title>Results</title>"
        "<p>Growth was unchanged <xref>[1]</xref>.</p></sec></body><back><ref-list><ref>Smith 2019</ref></ref-list></back></article>"
    )
    assert fulltext.jats_to_text(xml) == "Short summary.\n\n## Results\n\nGrowth was unchanged [1]."


def test_fetch_prefers_structured_sources_and_refuses_a_landing_page(monkeypatch):
    monkeypatch.setattr(fulltext, "pdf_to_text", lambda data: "From the PDF. " * 1000)
    asked = []

    def handler(request):
        asked.append(str(request.url))
        if "arxiv.org/html" in str(request.url):
            return httpx.Response(404)
        if str(request.url) == "https://publisher.example/paper":
            return httpx.Response(200, html="<meta content=https://publisher.example/paper.pdf name=citation_pdf_url><p>Abstract only.</p>")
        if str(request.url) == "https://bare.example/paper":
            return httpx.Response(200, html="<p>Abstract only.</p>")
        return httpx.Response(200, content=b"%PDF-1.7 ...")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    got = fulltext.fetch(Paper(id="arxiv:2201.11903", title="T"), client)
    assert got.source == "arXiv PDF" and asked == ["https://arxiv.org/html/2201.11903", "https://arxiv.org/pdf/2201.11903"]

    got = fulltext.fetch(Paper(id="W5", title="T", oa_url="https://publisher.example/paper"), client)
    assert got.text.startswith("From the PDF.") and asked[-1] == "https://publisher.example/paper.pdf"

    with pytest.raises(fulltext.FetchError, match="names no PDF"):
        fulltext.fetch(Paper(id="W6", title="T", oa_url="https://bare.example/paper"), client)
    with pytest.raises(fulltext.FetchError, match="no open copy is known"):
        fulltext.fetch(Paper(id="W7", title="T", oa_status="closed"), client)


def test_fetch_attaches_text_records_its_source_and_keeps_text_already_held(monkeypatch, capsys):
    monkeypatch.setattr(fulltext, "fetch", lambda paper: fulltext.Fetched(fulltext.html_to_text(BODY), "arXiv HTML", "https://arxiv.org/html/1"))
    monkeypatch.setattr(openalex, "get", lambda identifier: (_ for _ in ()).throw(openalex.OpenAlexError("budget spent")))
    store.attach_text("W2", "The text the passages were checked against.")

    assert main(["fetch", "W1", "W2", "W404"]) == 1
    first, second, third = json.loads(capsys.readouterr().out)
    assert first["source"] == "arXiv HTML" and store.load_paper("W1").text_source == "https://arxiv.org/html/1"
    assert "under hypoxia" in store.load_text("W1")
    assert second["source"] == "already attached" and store.load_text("W2").startswith("The text the passages")
    assert third == {"id": "W404", "ok": False, "why": "not cached"}

    assert main(["fetch", "W2", "--force"]) == 0
    assert "under hypoxia" in store.load_text("W2")


def test_the_openalex_key_comes_from_the_environment_or_a_file(tmp_path, monkeypatch):
    assert openalex.api_key() is None
    (tmp_path / "no-key").write_text("from-file\n")
    assert openalex.api_key() == "from-file"
    monkeypatch.setenv("OPENALEX_API_KEY", "from-env")
    assert openalex.api_key() == "from-env"


ATOM = """<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>Chain-of-Thought Prompting Elicits
 Reasoning</title><published>2022-01-28T00:00:00Z</published><summary>We explore.</summary>
<author><name>Jason Wei</name></author></entry></feed>"""


def test_an_arxiv_id_is_checked_against_arxivs_own_title():
    junk = {**WORK, "id": "https://openalex.org/W1", "title": "Pillars of a Systemic Revolution"}
    real = {**WORK, "id": "https://openalex.org/W2", "title": "Chain-of-thought prompting elicits reasoning", "open_access": {"oa_status": "closed"}, "best_oa_location": None}
    answers = {"by_title": [real]}

    def handler(request):
        if request.url.host == "export.arxiv.org":
            return httpx.Response(200, text=ATOM)
        if request.url.path.endswith("doi:10.48550/arXiv.2201.11903"):
            return httpx.Response(200, json=junk)
        return httpx.Response(200, json={"results": answers["by_title"]})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    paper, note = openalex.get("arxiv:2201.11903", client)
    assert paper.id == "W2" and "found by title" in note
    assert (paper.arxiv_id, paper.oa_status) == ("2201.11903", "green")

    answers["by_title"] = []
    paper, note = openalex.get("arxiv:2201.11903", client)
    assert paper.id == "arxiv:2201.11903" and paper.authors == ["Jason Wei"] and paper.year == 2022
    assert paper.title == "Chain-of-Thought Prompting Elicits Reasoning" and "made from arXiv" in note


def test_a_hand_added_paper_takes_its_openalex_record_everywhere(monkeypatch, capsys):
    store.save_paper(Paper(id="arxiv:2101.00001", title="Compound X under hypoxia", abstract=ABSTRACT))
    store.attach_text("arxiv:2101.00001", "Methods. Mice were kept at 1% oxygen throughout.")
    data = batch().model_dump()
    for item in data["nodes"] + data["edges"]:
        for p in item["provenance"]:
            p["paper_id"] = "arxiv:2101.00001"
    data["edges"][0]["provenance"] = prov("Mice were kept at 1% oxygen", "arxiv:2101.00001", "methods")
    sg = Subgraph(slug="q", question="Does X work?")
    assert store.add_batch(sg, Batch.model_validate(data)).ok
    store.save_subgraph(sg)
    pool = LocalPool()
    pool.push(sg)
    pool.add_leads([Lead(id="a-lead", claim="Compound X fails only when oxygen is low.", kind="other", confidence=0.3, nodes=["q/c:hypoxia", "q/o:no-reduction"],
                         checks=[{"question": "Is the premise in the source?", "finding": "Yes, in the methods.", "sources": ["arxiv:2101.00001"]}])])
    record = Paper(id="W99", title="Compound X under hypoxia", abstract="A different wording of the abstract.", oa_status="green")
    monkeypatch.setattr(openalex, "get", lambda identifier: (record, ""))

    wrong = Paper(id="W13", title="Pillars of a Systemic Revolution")
    monkeypatch.setattr(openalex, "get", lambda identifier: (wrong, ""))
    assert main(["paper", "reid", "arxiv:2101.00001"]) == 1 and "not 'Compound X under hypoxia'" in capsys.readouterr().out
    monkeypatch.setattr(openalex, "get", lambda identifier: (record, ""))

    assert main(["paper", "reid", "arxiv:2101.00001"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert (out["new"], out["subgraphs_rewritten"], out["pool_again"], out["leads_updated"]) == ("W99", ["q"], ["q"], ["a-lead"])

    sg = store.load_subgraph("q")
    assert set(sg.papers) == {"W99"}
    assert {p.paper_id for item in [*sg.nodes.values(), *sg.edges] for p in item.provenance} == {"W99"}
    assert store.load_paper("arxiv:2101.00001") is None and store.load_text("arxiv:2101.00001") is None
    assert "1% oxygen" in store.load_text("W99")
    assert store.load_paper("W99").abstract == ABSTRACT  # the wording the quoted passages were checked against
    assert pool.leads()[0].checks[0].sources == ["W99"]
    # Everything recorded still verifies under the new id.
    assert store.add_batch(Subgraph(slug="r", question="?"), Batch.model_validate(json.loads(json.dumps(data).replace("arxiv:2101.00001", "W99")))).ok


def test_a_question_can_be_framed_with_hypotheses_conditions_and_a_brief(tmp_path, capsys):
    assert main(["new", "told", "--question", "Does telling agents they are copies change how they coordinate?",
                 "--hypothesis", "Identical weights suffice; knowing about it adds nothing",
                 "--hypothesis", "h:common-knowledge-required=The gain appears only when both agents know they are copies",
                 "--condition", "c:told-partner-is-same-model=Agents are told their partner is the same model",
                 "--brief", "Cover superrationality and program equilibrium, not only papers about LLMs."]) == 0
    capsys.readouterr()
    sg = store.load_subgraph("told")
    assert list(sg.nodes) == ["h:identical-weights-suffice-knowing-about-it-adds-nothing", "h:common-knowledge-required", "c:told-partner-is-same-model"]
    assert all(node.framed for node in sg.nodes.values()) and sg.brief.startswith("Cover superrationality")

    # An extractor that reuses a framed id keeps the wording the question was posed with.
    data = batch().model_dump()
    data["nodes"].append({"id": "c:told-partner-is-same-model", "type": "condition", "label": "told same model"})
    data["edges"][1]["target"] = "c:told-partner-is-same-model"
    assert store.add_batch(sg, Batch.model_validate(data)).ok
    assert sg.nodes["c:told-partner-is-same-model"].label == "Agents are told their partner is the same model"
    store.save_subgraph(sg)

    assert main(["frame", "told", "--condition", "Agents are told nothing about their partner", "--hypothesis", "c:oops=Not a hypothesis"]) == 1
    assert "says otherwise" in capsys.readouterr().out
    assert main(["frame", "told", "--condition", "Agents are told nothing about their partner", "--hypothesis", "h:x-reduces-growth=X reduces growth"]) == 0
    capsys.readouterr()
    sg = store.load_subgraph("told")
    assert sg.nodes["h:x-reduces-growth"].framed and sg.nodes["h:x-reduces-growth"].label == "X reduces tumour growth"

    findings = "\n".join(lint(sg))
    assert "c:agents-are-told-nothing-about-their-partner: framed condition that no experiment is recorded under" in findings
    assert "h:common-knowledge-required: framed hypothesis with no evidence" in findings
    assert "c:told-partner-is-same-model" not in findings
    assert main(["show", "told"]) == 0
    shown = capsys.readouterr().out
    assert "brief: Cover superrationality" in shown and "[framed]" in shown
