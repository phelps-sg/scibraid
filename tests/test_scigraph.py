import json

import httpx
import pytest

import threading

from scigraph import main, openalex, store
from scigraph.cli import make_server
from scigraph.lint import lint
from scigraph.models import Batch, Paper, Subgraph
from scigraph.pool import HttpPool, LocalPool

ABSTRACT = (
    "We tested whether compound X reduces tumour growth in mice. Under hypoxic "
    "conditions, treated animals showed no reduction in tumour volume “relative to "
    "controls”. We suggest the effect reported earlier depends on oxygen tension."
)


@pytest.fixture(autouse=True)
def scigraph_home(tmp_path, monkeypatch):
    monkeypatch.setenv("SCIGRAPH_HOME", str(tmp_path))
    monkeypatch.delenv("SCIGRAPH_POOL_URL", raising=False)
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
        assert '"subgraphs": []' in httpx.get(url).text
        store.save_subgraph(Subgraph(slug="later", question="Added after the server started?"))
        assert "Added after the server started?" in httpx.get(url).text
        assert httpx.get(url + "etc/passwd").status_code == 404
    finally:
        server.shutdown()
        server.server_close()
