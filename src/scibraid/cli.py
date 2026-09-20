"""scibraid: deterministic tools for building and pooling evidence subgraphs.

The semantic work (reading, extracting, judging) is done by the agent driving
these commands; nothing here calls a model.
"""

from __future__ import annotations

import argparse
import json
import sys
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from collections import Counter
from pathlib import Path

import httpx
from pydantic import ValidationError

from . import align, embed, openalex, store
from .lint import lint
from .models import Alignment, Batch, Lead, Paper, Subgraph
from .pool import get_pool


def _emit(data: object) -> None:
    print(json.dumps(data, indent=2, ensure_ascii=False))


def _md_table(headers: list[str], rows: list[list[object]]) -> str:
    """A GitHub-flavoured Markdown table. Lists become line breaks within a cell."""

    def cell(value: object) -> str:
        if value is None:
            return ""
        if isinstance(value, (list, tuple, set)):
            value = "<br>".join(str(v) for v in value)
        elif isinstance(value, float):
            value = f"{value:.2f}"
        return str(value).replace("|", "\\|").replace("\n", " ").strip()

    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join(" --- " for _ in headers) + "|"]
    lines += ["| " + " | ".join(cell(v) for v in row) + " |" for row in rows]
    return "\n".join(lines)


def _errors(exc: ValidationError) -> list[str]:
    return [f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors()]


def cmd_search(args: argparse.Namespace) -> int:
    papers = openalex.search(args.query, args.limit, args.from_year, args.to_year, args.sort)
    for paper in papers:
        store.save_paper(paper)
    if args.full:
        _emit([p.model_dump(mode="json") for p in papers])
        return 0
    for p in papers:
        authors = (p.authors[0] + " et al.") if len(p.authors) > 1 else "".join(p.authors)
        print(f"{p.id}  {p.year}  cites={p.cited_by_count}  [{p.source_tier}]  {authors}")
        print(f"    {p.title}")
    print(f"\n{len(papers)} papers cached; read one with `scibraid paper show <id>`")
    return 0


def cmd_paper_show(args: argparse.Namespace) -> int:
    if (paper := store.load_paper(args.id)) is None:
        print(f"paper {args.id!r} is not cached", file=sys.stderr)
        return 1
    data = paper.model_dump(mode="json")
    data["fulltext_attached"] = store.load_text(args.id) is not None
    _emit(data)
    return 0


def cmd_paper_add(args: argparse.Namespace) -> int:
    try:
        paper = Paper.model_validate_json(Path(args.file).read_text())
    except ValidationError as exc:
        _emit({"ok": False, "errors": _errors(exc)})
        return 1
    store.save_paper(paper)
    print(f"cached {paper.id}")
    return 0


def cmd_paper_text(args: argparse.Namespace) -> int:
    if store.load_paper(args.id) is None:
        print(f"paper {args.id!r} is not cached", file=sys.stderr)
        return 1
    text = Path(args.file).read_text()
    store.attach_text(args.id, text)
    print(f"attached {len(text)} chars of full text to {args.id}")
    return 0


def cmd_new(args: argparse.Namespace) -> int:
    with store.subgraph_lock(args.slug):
        return _new(args)


def _new(args: argparse.Namespace) -> int:
    if store.subgraph_path(args.slug).exists():
        print(f"subgraph {args.slug!r} already exists", file=sys.stderr)
        return 1
    lead = None
    if args.lead:
        lead = next((x for x in get_pool().leads() if x.id == args.lead), None)
        if lead is None:
            print(f"no lead {args.lead!r} in the pool", file=sys.stderr)
            return 1
        if lead.status.value == "refuted":
            print(f"lead {lead.id!r} was refuted; there is nothing to follow up", file=sys.stderr)
            return 1
    question = args.question or (lead.follow_up if lead else None)
    if not question:
        print("give --question, or --lead for a lead that has a follow-up", file=sys.stderr)
        return 2
    try:
        sg = Subgraph(slug=args.slug, question=question, prompted_by=lead.id if lead else None)
        if lead:
            seed = align.derived_hypothesis(lead)
            sg.nodes[seed.id] = seed
    except ValidationError as exc:
        _emit({"ok": False, "errors": _errors(exc)})
        return 1
    store.save_subgraph(sg)
    print(store.subgraph_path(sg.slug))
    return 0


def cmd_add(args: argparse.Namespace) -> int:
    raw = sys.stdin.read() if args.batch == "-" else Path(args.batch).read_text()
    try:
        batch = Batch.model_validate_json(raw)
    except ValidationError as exc:
        _emit({"ok": False, "errors": _errors(exc)})
        return 1
    with store.subgraph_lock(args.slug):
        sg = store.load_subgraph(args.slug)
        report = store.add_batch(sg, batch)
        if report.ok:
            store.save_subgraph(sg)
    _emit({"ok": report.ok, **vars(report), "totals": _totals(sg)})
    return 0 if report.ok else 1


def _totals(sg: Subgraph) -> dict:
    return {"papers": len(sg.papers), "nodes": len(sg.nodes), "edges": len(sg.edges)}


def _mermaid(sg: Subgraph) -> str:
    shapes = {
        "hypothesis": ("([", "])"),
        "experiment": ("[", "]"),
        "condition": ("{{", "}}"),
        "observation": ("(", ")"),
        "interpretation": (">", "]"),
    }
    ids = {nid: f"n{i}" for i, nid in enumerate(sg.nodes)}
    lines = ["graph LR"]
    for nid, node in sg.nodes.items():
        left, right = shapes[node.type]
        label = node.label.replace('"', "'")
        if node.outcome:
            label += f" ({node.outcome})"
        lines.append(f'    {ids[nid]}{left}"{label}"{right}')
    for e in sg.edges:
        lines.append(f"    {ids[e.source]} -->|{e.relation} {e.confidence:.2f}| {ids[e.target]}")
    return "\n".join(lines)


def cmd_show(args: argparse.Namespace) -> int:
    sg = store.load_subgraph(args.slug)
    if args.format == "json":
        print(sg.model_dump_json(indent=2))
    elif args.format == "mermaid":
        print(_mermaid(sg))
    elif args.format == "markdown":
        print(f"## {sg.slug}\n\n{sg.question}\n")
        print(_md_table(["id", "type", "outcome", "label"], [[f"`{k}`", n.type.value, n.outcome.value if n.outcome else "", n.label] for k, n in sg.nodes.items()]))
        print()
        rows = [[f"`{e.source}`", e.relation.value, f"`{e.target}`", e.confidence, e.asserted_by.value, e.provenance[0].paper_id] for e in sg.edges]
        print(_md_table(["source", "relation", "target", "confidence", "asserted by", "paper"], rows))
    else:
        print(f"{sg.slug}: {sg.question}")
        print(f"  {_totals(sg)}  updated {sg.updated}")
        for kind, count in Counter(n.type for n in sg.nodes.values()).items():
            print(f"  {kind}: {count}")
        for nid, node in sg.nodes.items():
            outcome = f" [{node.outcome}]" if node.outcome else ""
            print(f"  {nid} ({node.type}){outcome}: {node.label}")
        for e in sg.edges:
            paper = e.provenance[0].paper_id
            print(
                f"  {e.source} -{e.relation}-> {e.target}"
                f"  conf={e.confidence:.2f} by={e.asserted_by} src={paper}"
            )
    return 0


def cmd_lint(args: argparse.Namespace) -> int:
    findings = lint(store.load_subgraph(args.slug))
    for finding in findings:
        print(f"- {finding}")
    print(f"{len(findings)} finding(s)")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    if args.format == "markdown":
        rows = [[f"`{sg.slug}`", sg.question, len(sg.papers), len(sg.nodes), len(sg.edges), sg.prompted_by or ""] for sg in store.list_subgraphs()]
        print(_md_table(["subgraph", "question", "papers", "nodes", "links", "prompted by lead"], rows))
        return 0
    for sg in store.list_subgraphs():
        print(f"{sg.slug}  {_totals(sg)}  {sg.question}")
    return 0


def render_view(subgraphs: list[Subgraph]) -> str:
    """A self-contained HTML page for browsing the given subgraphs and their alignments."""
    try:
        pool = get_pool()
        alignments = [x.model_dump(mode="json") for x in pool.alignments()]
        leads = [x.model_dump(mode="json") for x in pool.leads()]
    except httpx.HTTPError:
        alignments, leads = [], []
    payload = {"subgraphs": [sg.model_dump(mode="json") for sg in subgraphs], "alignments": alignments, "leads": leads}
    data = json.dumps(payload, ensure_ascii=False)
    # Embedded in a <script> block: keep any "</script>" or "<!--" in the data inert.
    data = data.replace("<", "\\u003c")
    template = Path(__file__).with_name("viewer.html").read_text()
    return template.replace("/*__DATA__*/", data)


def make_server(slugs: list[str], port: int) -> ThreadingHTTPServer:
    """Serve the viewer on localhost, re-rendering per request so a refresh shows new data."""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path.split("?")[0] != "/":
                self.send_error(404)
                return
            subgraphs = [store.load_subgraph(s) for s in slugs] if slugs else store.list_subgraphs()
            body = render_view(subgraphs).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: object) -> None:
            pass

    try:
        return ThreadingHTTPServer(("127.0.0.1", port), Handler)
    except OSError:
        return ThreadingHTTPServer(("127.0.0.1", 0), Handler)


def cmd_view(args: argparse.Namespace) -> int:
    for slug in args.slugs:
        store.load_subgraph(slug)
    if args.output:
        subgraphs = [store.load_subgraph(s) for s in args.slugs] if args.slugs else store.list_subgraphs()
        Path(args.output).write_text(render_view(subgraphs))
        print(args.output)
        return 0
    # Served rather than opened as a file: sandboxed browsers (snap, flatpak) cannot read
    # the data directory, and a served page can be refreshed as subgraphs grow.
    server = make_server(args.slugs, args.port)
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    print(f"serving {url}  (Ctrl-C to stop)")
    if not args.no_open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def cmd_pool(args: argparse.Namespace) -> int:
    pool = get_pool()
    if args.list:
        if args.format == "markdown":
            rows = [[f"`{r['slug']}`", r["question"], r["nodes"], r["edges"], r["pushed"]] for r in pool.listing()]
            print(_md_table(["subgraph", "question", "nodes", "links", "pooled"], rows))
        else:
            _emit(pool.listing())
        return 0
    if not args.slug:
        print("give a subgraph slug, or --list", file=sys.stderr)
        return 2
    sg = store.load_subgraph(args.slug)
    if not sg.edges:
        print(f"subgraph {sg.slug!r} has no edges; nothing worth pooling", file=sys.stderr)
        return 1
    print(f"pooled {sg.slug} ({_totals(sg)}) -> {pool.push(sg)}")
    if links := align.derivation_links(sg, pool.subgraphs()):
        pool.add_alignments(links)
        print(f"linked {len(links)} derived hypothesis link(s) back to the hypotheses the lead rested on")
    return 0


def cmd_candidates(args: argparse.Namespace) -> int:
    pool = get_pool()
    embedder = None if args.lexical else embed.default_embedder()
    if embedder is None and not args.lexical:
        print("embeddings not installed; ranking on word overlap only (install scibraid[embeddings])", file=sys.stderr)
    found = align.candidates(pool.subgraphs(), pool.alignments(), args.budget, args.min_plausibility, args.type, embedder)
    if args.format == "markdown":
        rows = [[c["priority"], c["plausibility"], c["type"], f"{c['a_node']['label']}<br>`{c['a']}`", f"{c['b_node']['label']}<br>`{c['b']}`"] for c in found]
        print(_md_table(["priority", "plausibility", "type", "a", "b"], rows))
    else:
        _emit(found)
    return 0


def cmd_hypotheses(args: argparse.Namespace) -> int:
    pool = get_pool()
    _emit(align.hypothesis_lists(pool.subgraphs(), pool.alignments()))
    return 0


def cmd_align_add(args: argparse.Namespace) -> int:
    pool = get_pool()
    raw = sys.stdin.read() if args.file == "-" else Path(args.file).read_text()
    try:
        verdicts = [Alignment.model_validate(item) for item in json.loads(raw)]
    except ValidationError as exc:
        _emit({"ok": False, "errors": _errors(exc)})
        return 1
    if errors := align.check_alignments(pool.subgraphs(), verdicts):
        _emit({"ok": False, "errors": errors})
        return 1
    pool.add_alignments(verdicts)
    _emit({"ok": True, "added": len(verdicts), "verdicts": Counter(v.verdict.value for v in verdicts)})
    return 0


def cmd_align_list(args: argparse.Namespace) -> int:
    if args.format == "markdown":
        rows = [[x.verdict.value, x.confidence, f"`{x.a}`", f"`{x.b}`", x.rationale] for x in get_pool().alignments()]
        print(_md_table(["verdict", "confidence", "a", "b", "rationale"], rows))
        return 0
    for x in get_pool().alignments():
        print(f"{x.verdict.value:9} {x.confidence:.2f}  {x.a}  ~  {x.b}")
        print(f"          {x.rationale}")
    return 0


def _print_observations(report: dict) -> None:
    print("pool:", report["pool"])
    for title, key in [
        ("Conditions reached independently from different questions", "bridging_conditions"),
        ("Failures sharing a condition across papers", "shared_condition_failures"),
        ("Results that may bear on another question's hypothesis", "cross_bearing"),
        ("Contradictions, and the conditions that differ", "contradictions_by_regime"),
        ("Experiments nobody ran: a linked hypothesis never tested under a condition that matters", "absent_experiments"),
        ("Hypotheses linked across questions", "linked_hypotheses"),
        ("Thinly evidenced hypotheses (fewer than 2 papers)", "thinly_evidenced_hypotheses"),
    ]:
        print(f"\n## {title} ({len(report[key])})")
        if len(report[key]) > 25:
            print("(first 25; use --format json for all)")
        for item in report[key][:25]:
            shown = {k: v for k, v in item.items() if k not in ("keys", "parts")}
            print("-", json.dumps(shown, ensure_ascii=False))


def cmd_lead_add(args: argparse.Namespace) -> int:
    pool = get_pool()
    raw = sys.stdin.read() if args.file == "-" else Path(args.file).read_text()
    try:
        leads = [Lead.model_validate(item) for item in json.loads(raw)]
    except ValidationError as exc:
        _emit({"ok": False, "errors": _errors(exc)})
        return 1
    if errors := align.check_leads(pool.subgraphs(), pool.alignments(), leads):
        _emit({"ok": False, "errors": errors})
        return 1
    # A lead is edited by reading it, changing it and adding it back. If someone else changed
    # it in between, adding this copy would silently discard their checks.
    current = {x.id: x for x in pool.leads()}
    stale = [
        f"lead {x.id}: changed by someone else since you read it (pool has {current[x.id].updated}, "
        f"yours says {x.updated if 'updated' in x.model_fields_set else 'nothing'}). "
        "Re-read it with `scibraid lead list --format json`, reapply your change, and keep its `updated` field"
        for x in leads
        if x.id in current and not ("updated" in x.model_fields_set and x.updated == current[x.id].updated)
    ]
    if stale:
        _emit({"ok": False, "errors": stale})
        return 1
    stamp = datetime.now(timezone.utc).isoformat(timespec="microseconds")
    for x in leads:
        x.updated = stamp
    pool.add_leads(leads)
    _emit({"ok": True, "added": len(leads), "status": Counter(x.status.value for x in leads)})
    return 0


def cmd_lead_list(args: argparse.Namespace) -> int:
    leads = [x for x in get_pool().leads() if not args.status or x.status.value == args.status]
    if args.format == "json":
        _emit([x.model_dump(mode="json") for x in leads])
        return 0
    if args.format == "markdown":
        rows = [[x.status.value, x.confidence, f"`{x.id}`", x.kind.value.replace("_", " "), x.claim, x.follow_up or ""] for x in leads]
        print(_md_table(["status", "confidence", "lead", "kind", "claim", "follow-up question"], rows))
        return 0
    for x in leads:
        print(f"{x.status.value:9} {x.confidence:.2f}  {x.id}  [{x.kind.value}]")
        print(f"          {x.claim}")
        if x.follow_up:
            print(f"          next: {x.follow_up}")
    return 0


def cmd_followups(args: argparse.Namespace) -> int:
    pool = get_pool()
    found = align.follow_ups(pool.leads(), pool.subgraphs(), store.list_subgraphs())
    if args.status:
        found = [f for f in found if f["status"] == args.status]
    if args.format == "json":
        _emit(found)
        return 0
    if args.format == "markdown":
        rows = [[f["status"].replace("_", " "), f["question"], f"`{f['lead']}`", f"{f['lead_status']} {f['lead_confidence']:.2f}", [f"`{s}`" for s in f["subgraphs"]]] for f in found]
        print(_md_table(["review", "question", "lead", "lead status", "reviewed in"], rows))
        return 0
    for f in found:
        built = f" -> {', '.join(f['subgraphs'])}" if f["subgraphs"] else ""
        print(f"{f['status']:11} [{f['lead_status']} {f['lead_confidence']:.2f}] {f['lead']}{built}")
        print(f"            {f['question']}")
    # Follow-ups are literature questions still to be read for. Open research questions need
    # an experiment, not more reading, so most have no follow-up and are not listed here.
    opened = sum(1 for x in pool.leads() if x.status.value == "open")
    print(f"\n{len(found)} follow-up question(s). {opened} open research question(s): see `scibraid agenda`.")
    return 0


def cmd_agenda(args: argparse.Namespace) -> int:
    pool = get_pool()
    found = align.agenda(pool.leads(), pool.subgraphs())
    if args.format == "json":
        _emit(found)
        return 0
    if args.format == "markdown":
        asked = lambda q: "not checked" if q["posed_in"] is None else (q["posed_in"] or "not found posed anywhere")
        rows = [[q["confidence"], q["question"], q["experiment_needed"], q["would_refute"], asked(q), [f"`{s}`" for s in q["literature_reviewed_in"]]] for q in found]
        print(_md_table(["confidence", "open question", "experiment needed", "would refute", "already asked?", "literature reviewed in"], rows))
        return 0
    for q in found:
        print(f"{q['confidence']:.2f}  {q['question']}")
        print(f"      experiment needed: {q['experiment_needed']}")
        print(f"      would refute:      {q['would_refute']}")
        reviewed = ", ".join(q["literature_reviewed_in"]) or "no literature question applied"
        print(f"      literature:        {reviewed}   (lead {q['lead']})")
        if q["posed_in"] is None:
            print("      already asked?     not checked")
        elif q["posed_in"]:
            print(f"      already asked in:  {'; '.join(q['posed_in'])}")
        else:
            print("      already asked?     not found posed anywhere")
    pending = sum(1 for f in align.follow_ups(pool.leads(), pool.subgraphs(), store.list_subgraphs()) if f["status"] == "pending")
    print(f"{len(found)} open research question(s). {pending} literature question(s) still to review: see `scibraid followups`.")
    return 0


def cmd_repair_list(args: argparse.Namespace) -> int:
    found = align.open_repairs(get_pool().leads())
    if args.format == "json":
        _emit(found)
        return 0
    if args.format == "markdown":
        rows = [[r["kind"], f"`{r['target']}`", r["problem"], f"`{r['lead']}` #{r['index']}"] for r in found]
        print(_md_table(["kind", "target", "problem", "found by lead"], rows))
        return 0
    for r in found:
        print(f"{r['kind']:10} {r['target']}   (lead {r['lead']}, #{r['index']})")
        print(f"           {r['problem']}")
    print(f"{len(found)} open repair(s)")
    return 0


def cmd_repair_resolve(args: argparse.Namespace) -> int:
    pool = get_pool()
    lead = next((x for x in pool.leads() if x.id == args.lead), None)
    if lead is None or not 0 <= args.index < len(lead.repairs):
        print(f"no repair #{args.index} on lead {args.lead!r}", file=sys.stderr)
        return 1
    lead.repairs[args.index].resolved = True
    lead.repairs[args.index].resolution = args.note
    lead.updated = datetime.now(timezone.utc).isoformat(timespec="microseconds")
    pool.add_leads([lead])
    print(f"resolved repair #{args.index} on {lead.id}")
    return 0


def _print_observations_markdown(report: dict) -> None:
    covered = lambda item: [f"`{x['id']}` ({x['status']})" for x in item.get("leads", [])]
    by_slug = lambda d: [f"{slug}: {len(v) if isinstance(v, list) else v}" for slug, v in d.items()]
    sections = [
        ("Conditions reached from different questions", "bridging_conditions", ["condition", "specificity", "experiments", "covered by"],
         lambda i: [i["condition"], i["specificity"], by_slug(i["experiments"]), covered(i)]),
        ("Failures sharing a condition", "shared_condition_failures", ["condition", "specificity", "papers", "results", "covered by"],
         lambda i: [i["condition"], i["specificity"], i["papers"], i["observations"], covered(i)]),
        ("Experiments that may bear on another question's hypothesis", "cross_bearing", ["experiment", "from", "may bear on", "in", "shared conditions", "score", "covered by"],
         lambda i: [i["experiment"], i["from"], i["may_bear_on"], i["in"], i["shared_conditions"], i["score"], covered(i)]),
        ("Contradictions, and the conditions that differ", "contradictions_by_regime", ["result a", "result b", "shared", "only a", "only b", "covered by"],
         lambda i: [i["a"], i["b"], i["shared_conditions"], i["only_a"], i["only_b"], covered(i)]),
        ("Experiments nobody ran", "absent_experiments", ["hypothesis", "in", "never tested under", "which scope results on", "judged", "covered by"],
         lambda i: [i["hypothesis"], i["in"], i["never_tested_under"], i["which_scope_results_on"], f"{i['hypotheses_judged']} {i['confidence']:.2f}", covered(i)]),
        ("Hypotheses linked across questions", "linked_hypotheses", ["verdict", "confidence", "a", "b", "rationale"],
         lambda i: [i["verdict"], i["confidence"], f"{i['a']['hypothesis']} ({i['a']['in']})", f"{i['b']['hypothesis']} ({i['b']['in']})", i["rationale"]]),
        ("Thinly evidenced hypotheses", "thinly_evidenced_hypotheses", ["hypothesis", "in", "supports", "contradicts", "papers"],
         lambda i: [i["hypothesis"], i["in"], i["supports"], i["contradicts"], i["papers"]]),
    ]
    pool = report["pool"]
    print(f"{pool['subgraphs']} subgraphs, {pool['nodes']} nodes, {pool['alignments']} alignment judgements.\n")
    for title, key, headers, row in sections:
        items = report[key]
        print(f"### {title} ({len(items)})\n")
        if items:
            print(_md_table(headers, [row(i) for i in items[:25]]))
            if len(items) > 25:
                print(f"\nFirst 25 of {len(items)}; use `--format json` for all.")
        print()


def cmd_observe(args: argparse.Namespace) -> int:
    pool = get_pool()
    report = align.observe(pool.subgraphs(), pool.alignments(), args.min_confidence)
    report = align.mark_covered(report, pool.leads(), only_new=args.new)
    if args.format == "json":
        _emit(report)
    elif args.format == "markdown":
        _print_observations_markdown(report)
    else:
        _print_observations(report)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="scibraid", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("search", help="search OpenAlex and cache the results")
    p.add_argument("query")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--from-year", type=int)
    p.add_argument("--to-year", type=int)
    p.add_argument("--sort", default="relevance_score:desc", help="e.g. cited_by_count:desc")
    p.add_argument("--full", action="store_true", help="emit JSON including abstracts")
    p.set_defaults(func=cmd_search)

    paper = sub.add_parser("paper", help="inspect or extend the paper cache")
    paper_sub = paper.add_subparsers(dest="paper_command", required=True)
    p = paper_sub.add_parser("show", help="print a cached paper, abstract included")
    p.add_argument("id")
    p.set_defaults(func=cmd_paper_show)
    p = paper_sub.add_parser("add", help="cache a paper from a JSON file (non-OpenAlex sources)")
    p.add_argument("file")
    p.set_defaults(func=cmd_paper_add)
    p = paper_sub.add_parser("text", help="attach full text, so passages can be verified")
    p.add_argument("id")
    p.add_argument("file")
    p.set_defaults(func=cmd_paper_text)

    p = sub.add_parser("new", help="start a subgraph for a research question")
    p.add_argument("slug")
    p.add_argument("--question", help="defaults to the lead's follow-up when --lead is given")
    p.add_argument("--lead", help="id of the lead this subgraph follows up; seeds its claim as a derived hypothesis")
    p.set_defaults(func=cmd_new)

    p = sub.add_parser("add", help="validate a batch of nodes and edges and apply it")
    p.add_argument("slug")
    p.add_argument("batch", help="JSON file, or - for stdin")
    p.set_defaults(func=cmd_add)

    p = sub.add_parser("show", help="print a subgraph")
    p.add_argument("slug")
    p.add_argument("--format", choices=["summary", "json", "mermaid", "markdown"], default="summary")
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("lint", help="structural checks: what to look at again")
    p.add_argument("slug")
    p.set_defaults(func=cmd_lint)

    p = sub.add_parser("list", help="list local subgraphs")
    p.add_argument("--format", choices=["text", "markdown"], default="text")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("view", help="browse subgraphs in the browser (all local ones by default)")
    p.add_argument("slugs", nargs="*")
    p.add_argument("-o", "--output", help="write a self-contained HTML file instead of serving")
    p.add_argument("--port", type=int, default=8765, help="localhost port (falls back to a free one)")
    p.add_argument("--no-open", action="store_true", help="serve without opening a browser")
    p.set_defaults(func=cmd_view)

    p = sub.add_parser("pool", help="push a subgraph to the pool, or list the pool")
    p.add_argument("slug", nargs="?")
    p.add_argument("--list", action="store_true")
    p.add_argument("--format", choices=["json", "markdown"], default="json", help="for --list")
    p.set_defaults(func=cmd_pool)

    p = sub.add_parser("candidates", help="rank unjudged cross-subgraph node pairs in the pool")
    p.add_argument("--budget", type=int, default=40, help="how many pairs to propose")
    p.add_argument("--min-plausibility", type=float, default=0.3)
    p.add_argument("--type", choices=["hypothesis", "experiment", "condition", "observation", "interpretation"])
    p.add_argument("--lexical", action="store_true", help="rank on word overlap only, even if embeddings are installed")
    p.add_argument("--format", choices=["json", "markdown"], default="json")
    p.set_defaults(func=cmd_candidates)

    p = sub.add_parser("hypotheses", help="both hypothesis lists for each pair of pooled subgraphs, to be read in full")
    p.set_defaults(func=cmd_hypotheses)

    al = sub.add_parser("align", help="record or list alignment verdicts")
    al_sub = al.add_subparsers(dest="align_command", required=True)
    p = al_sub.add_parser("add", help="validate a JSON list of verdicts and store it in the pool")
    p.add_argument("file", help="JSON file, or - for stdin")
    p.set_defaults(func=cmd_align_add)
    p = al_sub.add_parser("list", help="print the pool's alignment verdicts")
    p.add_argument("--format", choices=["text", "markdown"], default="text")
    p.set_defaults(func=cmd_align_list)

    ld = sub.add_parser("lead", help="record or list leads drawn from the pool")
    ld_sub = ld.add_subparsers(dest="lead_command", required=True)
    p = ld_sub.add_parser("add", help="validate a JSON list of leads and store it in the pool")
    p.add_argument("file", help="JSON file, or - for stdin")
    p.set_defaults(func=cmd_lead_add)
    p = ld_sub.add_parser("list", help="print the pool's leads")
    p.add_argument("--status", choices=["candidate", "holds", "open", "known", "refuted"])
    p.add_argument("--format", choices=["text", "json", "markdown"], default="text")
    p.set_defaults(func=cmd_lead_list)

    p = sub.add_parser("followups", help="leads' follow-up questions and whether a subgraph answers each")
    p.add_argument("--status", choices=["pending", "in_progress", "reviewed"])
    p.add_argument("--format", choices=["text", "json", "markdown"], default="text")
    p.set_defaults(func=cmd_followups)

    p = sub.add_parser("agenda", help="open research questions: claims the literature cannot settle, and the experiment each needs")
    p.add_argument("--format", choices=["text", "json", "markdown"], default="text")
    p.set_defaults(func=cmd_agenda)

    rp = sub.add_parser("repair", help="faults in subgraphs or verdicts that checking a lead turned up")
    rp_sub = rp.add_subparsers(dest="repair_command", required=True)
    p = rp_sub.add_parser("list", help="open repairs")
    p.add_argument("--format", choices=["text", "json", "markdown"], default="text")
    p.set_defaults(func=cmd_repair_list)
    p = rp_sub.add_parser("resolve", help="mark a repair done")
    p.add_argument("lead")
    p.add_argument("index", type=int)
    p.add_argument("--note", required=True, help="what was changed")
    p.set_defaults(func=cmd_repair_resolve)

    p = sub.add_parser("observe", help="read candidate observations off the aligned pool")
    p.add_argument("--min-confidence", type=float, default=0.7, help="'same' verdicts below this are ignored")
    p.add_argument("--new", action="store_true", help="hide candidates that a recorded lead already covers")
    p.add_argument("--format", choices=["text", "json", "markdown"], default="text")
    p.set_defaults(func=cmd_observe)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except FileNotFoundError as exc:
        print(exc, file=sys.stderr)
        return 1
    except httpx.HTTPError as exc:
        print(f"network error: {exc}", file=sys.stderr)
        return 1
