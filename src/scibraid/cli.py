"""scibraid: deterministic tools for building and pooling evidence subgraphs.

The semantic work (reading, extracting, judging) is done by the agent driving
these commands; nothing here calls a model.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from collections import Counter
from pathlib import Path

import httpx
from pydantic import ValidationError

from . import align, bibtex, draft, embed, fulltext, identity, openalex, store
from .lint import lint
from .models import Alignment, Batch, Builder, Lead, Paper, Subgraph
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


def _who(builders: list[Builder]) -> list[str]:
    return [" / ".join(part for part in (b.person, b.model or "model unknown", b.agent) if part) for b in builders]


def _builder(args: argparse.Namespace) -> Builder:
    builder = identity.current_builder(getattr(args, "model", None))
    if builder.model is None:
        print("note: no model recorded for this work; pass --model <model id> or set SCIBRAID_MODEL", file=sys.stderr)
    return builder


def _errors(exc: ValidationError) -> list[str]:
    return [f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors()]


def _access(paper: Paper) -> str:
    if paper.oa_status is None:
        return "access unknown"
    return "closed" if paper.oa_status == "closed" else f"open ({paper.oa_status})"


def cmd_search(args: argparse.Namespace) -> int:
    try:
        papers = openalex.search(
            args.query, args.limit, args.from_year, args.to_year, args.sort,
            match=args.match, citing=args.citing, references_of=args.references_of,
        )
    except openalex.OpenAlexError as exc:
        print(exc, file=sys.stderr)
        return 1
    for paper in papers:
        store.save_paper(_keeping_text_source(paper))
    if args.full:
        _emit([p.model_dump(mode="json") for p in papers])
        return 0
    for p in papers:
        authors = (p.authors[0] + " et al.") if len(p.authors) > 1 else "".join(p.authors)
        flags = f"[{p.source_tier}] [{_access(p)}]" + ("" if p.abstract else " [no abstract]")
        print(f"{p.id}  {p.year}  cites={p.cited_by_count}  {flags}  {authors}")
        print(f"    {p.title}")
    closed = sum(p.oa_status == "closed" for p in papers)
    print(f"\n{len(papers)} papers cached ({closed} closed); read one with `scibraid paper show <id>`")
    return 0


def _keeping_text_source(paper: Paper) -> Paper:
    """Fresh metadata must not forget where an already attached full text came from."""
    if (held := store.load_paper(paper.id)) is not None and held.text_source:
        paper.text_source = held.text_source
    return paper


def cmd_paper_get(args: argparse.Namespace) -> int:
    failed = 0
    for identifier in args.identifiers:
        try:
            paper, note = openalex.get(identifier)
        except openalex.OpenAlexError as exc:
            print(f"{identifier}: {exc}", file=sys.stderr)
            failed += 1
            continue
        store.save_paper(_keeping_text_source(paper))
        print(f"{paper.id}  {paper.year}  [{_access(paper)}]  {paper.title}" + (f"\n    note: {note}" if note else ""))
    return 1 if failed else 0


def cmd_paper_reid(args: argparse.Namespace) -> int:
    if store.load_paper(args.old) is None:
        print(f"paper {args.old!r} is not cached", file=sys.stderr)
        return 1
    try:
        paper, note = openalex.get(args.identifier or args.old)
    except openalex.OpenAlexError as exc:
        print(f"{args.old}: {exc}", file=sys.stderr)
        return 1
    held = store.load_paper(args.old)
    if not args.force and not openalex._alike(held.title, paper.title):
        _emit({"ok": False, "old": args.old, "new": paper.id, "errors": [
            f"the record found is titled {paper.title!r}, not {held.title!r}; name the right work as a second argument, or pass --force"]})
        return 1
    rewritten, errors = store.reidentify_paper(args.old, paper)
    if errors:
        _emit({"ok": False, "old": args.old, "new": paper.id, "errors": errors})
        return 1
    # Leads name the papers they consulted by id; pooled subgraphs are copies and need pooling again.
    pool = get_pool()
    changed = []
    for lead in pool.leads():
        before = lead.model_dump_json()
        for check in lead.checks:
            check.sources = [paper.id if src == args.old else src for src in check.sources]
        lead.known_in = [text.replace(args.old, paper.id) for text in lead.known_in]
        if lead.posed_in is not None:
            lead.posed_in = [text.replace(args.old, paper.id) for text in lead.posed_in]
        if lead.model_dump_json() != before:
            changed.append(lead)
    if changed:
        pool.add_leads(changed)
    pooled = {sg.slug for sg in pool.subgraphs()}
    _emit({"ok": True, "old": args.old, "new": paper.id, "note": note, "subgraphs_rewritten": rewritten,
           "pool_again": sorted(pooled & set(rewritten)), "leads_updated": [lead.id for lead in changed]})
    return 0


def cmd_fetch(args: argparse.Namespace) -> int:
    ids = list(args.ids)
    if args.subgraph:
        ids += [pid for pid in store.load_subgraph(args.subgraph).papers if pid not in ids]
    if not ids:
        print("name papers, or a subgraph with --subgraph", file=sys.stderr)
        return 1
    results = []
    for paper_id in ids:
        if (paper := store.load_paper(paper_id)) is None:
            results.append({"id": paper_id, "ok": False, "why": "not cached"})
            continue
        # Passages already recorded were checked against the text that is held, so it is not replaced unasked.
        if store.load_text(paper_id) is not None and not args.force:
            results.append({"id": paper_id, "ok": True, "source": "already attached"})
            continue
        if paper.oa_status is None and paper.id.startswith("W"):
            try:  # cached before open-access locations were recorded
                paper = _keeping_text_source(openalex.get(paper.id)[0])
            except openalex.OpenAlexError:
                pass
        if results and results[-1].get("url"):
            time.sleep(3)  # arXiv asks for no more than one request every three seconds
        try:
            got = fulltext.fetch(paper)
        except fulltext.FetchError as exc:
            results.append({"id": paper_id, "ok": False, "access": _access(paper), "why": str(exc)})
            continue
        store.attach_text(paper_id, got.text)
        paper.text_source = got.url
        store.save_paper(paper)
        results.append({"id": paper_id, "ok": True, "source": got.source, "url": got.url, "chars": len(got.text)})
    _emit(results)
    return 0 if all(r["ok"] for r in results) else 1


def cmd_paper_show(args: argparse.Namespace) -> int:
    if (paper := store.load_paper(args.id)) is None:
        print(f"paper {args.id!r} is not cached", file=sys.stderr)
        return 1
    text = store.load_text(args.id)
    if args.text:
        if text is None:
            print(f"no full text is attached to {args.id}; try `scibraid fetch {args.id}`", file=sys.stderr)
            return 1
        print(text)
        return 0
    data = paper.model_dump(mode="json")
    data["fulltext_attached"] = text is not None
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
        sg = Subgraph(slug=args.slug, question=question, prompted_by=lead.id if lead else None, builders=[_builder(args)])
        if lead:
            seed = align.derived_hypothesis(lead)
            sg.nodes[seed.id] = seed
        errors = store.frame(sg, args.hypothesis, args.condition, args.brief)
    except ValidationError as exc:
        _emit({"ok": False, "errors": _errors(exc)})
        return 1
    if errors:
        _emit({"ok": False, "errors": errors})
        return 1
    store.save_subgraph(sg)
    print(store.subgraph_path(sg.slug))
    return 0


def cmd_frame(args: argparse.Namespace) -> int:
    with store.subgraph_lock(args.slug):
        sg = store.load_subgraph(args.slug)
        try:
            errors = store.frame(sg, args.hypothesis, args.condition, args.brief)
        except ValidationError as exc:
            errors = _errors(exc)
        if not errors:
            store.save_subgraph(sg)
    framed = {nid: node.label for nid, node in sg.nodes.items() if node.framed}
    _emit({"ok": not errors, "errors": errors, "brief": sg.brief, "framed": framed})
    return 1 if errors else 0


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
            builder = _builder(args)
            if builder not in sg.builders:
                sg.builders.append(builder)
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
        if sg.brief:
            print(f"Brief: {sg.brief}\n")
        print(_md_table(["id", "type", "outcome", "label"], [[f"`{k}`", n.type.value, n.outcome.value if n.outcome else "", n.label] for k, n in sg.nodes.items()]))
        print()
        rows = [[f"`{e.source}`", e.relation.value, f"`{e.target}`", e.confidence, e.asserted_by.value, e.provenance[0].paper_id] for e in sg.edges]
        print(_md_table(["source", "relation", "target", "confidence", "asserted by", "paper"], rows))
    else:
        print(f"{sg.slug}: {sg.question}")
        if sg.brief:
            print(f"  brief: {sg.brief}")
        print(f"  {_totals(sg)}  updated {sg.updated}")
        for kind, count in Counter(n.type for n in sg.nodes.values()).items():
            print(f"  {kind}: {count}")
        for nid, node in sg.nodes.items():
            outcome = f" [{node.outcome}]" if node.outcome else ""
            outcome += " [framed]" if node.framed else ""
            print(f"  {nid} ({node.type}){outcome}: {node.label}")
        for e in sg.edges:
            paper = e.provenance[0].paper_id
            print(
                f"  {e.source} -{e.relation}-> {e.target}"
                f"  conf={e.confidence:.2f} by={e.asserted_by} src={paper}"
            )
        for r in sg.retracted:
            what = f"node {r.node.id} and {len(r.edges)} edge(s)" if r.node else "; ".join(f"{e.source} -{e.relation}-> {e.target}" for e in r.edges)
            print(f"  retracted: {what}  ({r.reason})")
    return 0


def cmd_lint(args: argparse.Namespace) -> int:
    findings = lint(store.load_subgraph(args.slug))
    for finding in findings:
        print(f"- {finding}")
    print(f"{len(findings)} finding(s)")
    return 0


def cmd_duplicates(args: argparse.Namespace) -> int:
    embedder = None if args.lexical else embed.default_embedder()
    found = align.duplicates(store.load_subgraph(args.slug), args.min_score, embedder)
    if args.format == "markdown":
        print(_md_table(["score", "a", "b"], [[d["score"], f"`{d['a']}` {d['a_label']}", f"`{d['b']}` {d['b_label']}"] for d in found]))
    else:
        _emit(found)
    return 0


def cmd_merge(args: argparse.Namespace) -> int:
    with store.subgraph_lock(args.slug):
        sg = store.load_subgraph(args.slug)
        errors = store.merge_nodes(sg, args.keep, args.drop)
        if not errors:
            builder = _builder(args)
            if builder not in sg.builders:
                sg.builders.append(builder)
            store.save_subgraph(sg)
    if errors:
        _emit({"ok": False, "errors": errors})
        return 1
    # Verdicts and leads name nodes by id, so a pooled node that disappears leaves them dangling.
    gone = f"{args.slug}/{args.drop}"
    pool = get_pool()
    dangling = [f"verdict {x.a} ~ {x.b}" for x in pool.alignments() if gone in (x.a, x.b)]
    dangling += [f"lead {lead.id}" for lead in pool.leads() if gone in lead.nodes]
    _emit({"ok": True, "kept": args.keep, "dropped": args.drop, "totals": _totals(sg), "now_dangling_in_pool": dangling})
    return 0


def cmd_retract(args: argparse.Namespace) -> int:
    with store.subgraph_lock(args.slug):
        sg = store.load_subgraph(args.slug)
        builder = _builder(args)
        removed, errors = store.retract(sg, args.reason, builder, node=args.node, edge=args.edge, paper=args.paper)
        if not errors:
            if builder not in sg.builders:
                sg.builders.append(builder)
            store.save_subgraph(sg)
    if errors:
        _emit({"ok": False, "errors": errors})
        return 1
    dangling = []
    if args.node:  # verdicts and leads name nodes by id
        gone = f"{args.slug}/{args.node}"
        pool = get_pool()
        dangling = [f"verdict {x.a} ~ {x.b}" for x in pool.alignments() if gone in (x.a, x.b)]
        dangling += [f"lead {lead.id}" for lead in pool.leads() if gone in lead.nodes]
    _emit({"ok": True, "edges_removed": removed, "node_removed": args.node, "totals": _totals(sg),
           "retractions_on_record": len(sg.retracted), "now_dangling_in_pool": dangling})
    return 0


def cmd_voice_set(args: argparse.Namespace) -> int:
    source = Path(args.source).expanduser()
    if source.exists():
        kind = source.suffix.lower()
        text = (fulltext.pdf_to_text(source.read_bytes()) if kind == ".pdf"
                else fulltext.html_to_text(source.read_text()) if kind in (".html", ".htm") else source.read_text())
        meta = {"title": source.stem, "source": str(source)}
    else:
        try:
            paper, _ = openalex.get(args.source)
            got = fulltext.fetch(paper)
        except (openalex.OpenAlexError, fulltext.FetchError) as exc:
            print(f"{args.source}: {exc}\nGive a file instead (.pdf, .txt, .tex, .md or .html).", file=sys.stderr)
            return 1
        text, meta = got.text, {"title": paper.title, "authors": paper.authors, "year": paper.year, "source": got.url}
    if len(text) < 5000:
        print(f"only {len(text)} characters of text: too little to take a voice from", file=sys.stderr)
        return 1
    name = args.name or "-".join(re.findall(r"[a-z0-9]+", meta["title"].lower())[:4])
    _emit(draft.save_voice(name, text, meta, args.default))
    return 0


def cmd_voice_list(args: argparse.Namespace) -> int:
    _emit(draft.voices())
    return 0


def cmd_voice_show(args: argparse.Namespace) -> int:
    if (found := draft.voice(args.name)) is None:
        print("no such voice exemplar" if args.name else "no voice exemplar is set; add one with `scibraid voice set`", file=sys.stderr)
        return 1
    meta, path = found
    _emit({**meta, "text": str(path)})
    return 0


def _draft_state(directory: Path) -> dict:
    path = directory / "draft.json"
    return json.loads(path.read_text()) if path.exists() else {}


def _write_draft(directory: Path, state: dict) -> dict:
    pool = get_pool()
    subgraphs = [sg for sg in pool.subgraphs() if sg.slug in state["slugs"]]
    papers = {pid: p for sg in subgraphs for pid, p in sg.papers.items()}
    papers.update({pid: p for pid in state.get("extra", []) if (p := store.load_paper(pid)) is not None})
    state["keys"] = draft.assign_keys(state.get("keys", {}), list(papers.values()))
    embedder = embed.default_embedder() if state.get("focus") else None
    files = draft.dossier(subgraphs, pool.alignments(), pool.leads(), state["keys"], state.get("focus", ""), embedder)
    for stale in directory.glob("evidence-*.md"):
        stale.unlink()
    for name, content in files.items():
        (directory / name).write_text(content)
    text = "".join(files.values())
    (directory / "refs.bib").write_text(draft.bib(state["keys"], papers))
    (directory / "draft.json").write_text(json.dumps(state, indent=2))
    return {"dossier": str(directory / "dossier.md"), "overview_words": len(files["dossier.md"].split()),
            "evidence_files": {name: len(content.split()) for name, content in files.items() if name != "dossier.md"},
            "dossier_words": len(text.split()), "references": len(state["keys"])}


def cmd_draft_start(args: argparse.Namespace) -> int:
    pooled = {sg.slug for sg in get_pool().subgraphs()}
    if missing := [s for s in args.slugs if s not in pooled]:
        print(f"not in the pool: {', '.join(missing)} (a write-up rests on pooled subgraphs; `scibraid pool --list`)", file=sys.stderr)
        return 1
    directory = Path(args.dir).expanduser()
    directory.mkdir(parents=True, exist_ok=True)
    state = _draft_state(directory)
    state.update({"slugs": args.slugs, "focus": args.focus if args.focus is not None else state.get("focus", ""), "started": state.get("started") or datetime.now(timezone.utc).isoformat(timespec="seconds")})
    if args.voice or "voice" not in state:
        found = draft.voice(args.voice)
        if args.voice and found is None:
            print(f"no voice exemplar called {args.voice!r}; see `scibraid voice list`", file=sys.stderr)
            return 1
        state["voice"] = found[0]["name"] if found else None
    report = _write_draft(directory, state)
    if not (directory / "main.tex").exists():
        (directory / "main.tex").write_text(draft.TEMPLATE)
    found = draft.voice(state["voice"]) if state["voice"] else None
    _emit({"ok": True, "dir": str(directory), **report, "main": str(directory / "main.tex"), "focus": state["focus"],
           "voice": {"name": found[0]["name"], "title": found[0].get("title"), "text": str(found[1])} if found else None})
    return 0


def cmd_draft_cite(args: argparse.Namespace) -> int:
    directory = Path(args.dir).expanduser()
    state = _draft_state(directory)
    if not state:
        print(f"{directory} has no draft.json; start with `scibraid draft start`", file=sys.stderr)
        return 1
    added, failed = [], 0
    for identifier in args.identifiers:
        paper = store.load_paper(identifier)
        note = ""
        if paper is None:
            try:
                paper, note = openalex.get(identifier)
            except openalex.OpenAlexError as exc:
                print(f"{identifier}: {exc}", file=sys.stderr)
                failed += 1
                continue
            store.save_paper(_keeping_text_source(paper))
        if paper.id not in state.setdefault("extra", []):
            state["extra"].append(paper.id)
        added.append((paper, note))
    _write_draft(directory, state)
    _emit([{"key": state["keys"][p.id], "id": p.id, "title": p.title, "year": p.year, **({"note": note} if note else {})} for p, note in added])
    return 1 if failed else 0


def cmd_draft_check(args: argparse.Namespace) -> int:
    report = draft.check(Path(args.dir).expanduser(), compile_it=not args.no_compile)
    _emit(report)
    return 0 if report["ok"] else 1


def cmd_list(args: argparse.Namespace) -> int:
    if args.format == "markdown":
        rows = [[f"`{sg.slug}`", sg.question, len(sg.papers), len(sg.nodes), len(sg.edges), _who(sg.builders), sg.prompted_by or ""] for sg in store.list_subgraphs()]
        print(_md_table(["subgraph", "question", "papers", "nodes", "links", "built by", "prompted by lead"], rows))
        return 0
    for sg in store.list_subgraphs():
        print(f"{sg.slug}  {_totals(sg)}  {sg.question}")
        print(f"    built by: {'; '.join(_who(sg.builders)) or 'not recorded'}")
    return 0


def render_view(subgraphs: list[Subgraph]) -> str:
    """A self-contained HTML page for browsing the given subgraphs and their alignments."""
    try:
        pool = get_pool()
        alignments = [x.model_dump(mode="json") for x in pool.alignments()]
        leads = [x.model_dump(mode="json") for x in pool.leads()]
    except httpx.HTTPError:
        alignments, leads = [], []
    papers = {p.id: p for sg in subgraphs for p in sg.papers.values()}
    payload = {
        "subgraphs": [sg.model_dump(mode="json") for sg in subgraphs],
        "alignments": alignments,
        "leads": leads,
        "bibtex": bibtex.bibliography(list(papers.values())),
    }
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


def cmd_bibtex(args: argparse.Namespace) -> int:
    subgraphs = [store.load_subgraph(s) for s in args.slugs] if args.slugs else store.list_subgraphs()
    papers = {p.id: p for sg in subgraphs for p in sg.papers.values()}
    text = "\n\n".join(item["entry"] for item in bibtex.bibliography(list(papers.values())).values()) + "\n"
    if args.output:
        Path(args.output).write_text(text)
        print(f"{len(papers)} entries -> {args.output}")
    else:
        print(text, end="")
    return 0


def cmd_builder_add(args: argparse.Namespace) -> int:
    """Record a builder after the fact, for subgraphs made before builders were recorded."""
    with store.subgraph_lock(args.slug):
        sg = store.load_subgraph(args.slug)
        builder = Builder(person=args.person, agent=args.agent, model=args.model, session=args.session)
        if builder not in sg.builders:
            sg.builders.append(builder)
            store.save_subgraph(sg)
    print(f"{sg.slug} built by: {'; '.join(_who(sg.builders))}   (pool it again to share this)")
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
    judge = _builder(args)
    for verdict in verdicts:
        verdict.judge = judge
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
    checker = _builder(args)
    for x in leads:
        x.updated = stamp
        x.by = checker
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
        rows = [[q["confidence"], q["question"], q["experiment_needed"], q["would_refute"], asked(q), [f"`{s}`" for s in q["literature_reviewed_in"]],
                 q["checker_vs_builders"]] for q in found]
        print(_md_table(["confidence", "open question", "experiment needed", "would refute", "already asked?", "literature reviewed in", "checker vs builders"], rows))
        return 0
    for q in found:
        print(f"{q['confidence']:.2f}  {q['question']}")
        print(f"      experiment needed: {q['experiment_needed']}")
        print(f"      would refute:      {q['would_refute']}")
        reviewed = ", ".join(q["literature_reviewed_in"]) or "no literature question applied"
        print(f"      literature:        {reviewed}   (lead {q['lead']})")
        print(f"      checked by:        {q['checked_by'] or 'not recorded'}  (against the builders of its evidence: {q['checker_vs_builders']})")
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


def _framing_arguments(p: argparse.ArgumentParser) -> None:
    p.add_argument("--hypothesis", action="append", default=[], metavar="TEXT",
                   help='a hypothesis to test, repeatable; "h:my-id=Label" fixes the id')
    p.add_argument("--condition", action="append", default=[], metavar="TEXT",
                   help='a distinction every extractor should record, repeatable; "c:my-id=Label" fixes the id')
    p.add_argument("--brief", help="how to steer the search and extraction: literatures to cover, distinctions to keep")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="scibraid", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("search", help="search OpenAlex and cache the results")
    p.add_argument("query", nargs="?", help="optional when --citing or --references-of is given")
    p.add_argument("--match", choices=["title-abstract", "anywhere"], default="title-abstract",
                   help="'anywhere' also matches full text, which OpenAlex holds only for open papers, so it favours them")
    p.add_argument("--citing", metavar="ID", help="only works that cite this OpenAlex id")
    p.add_argument("--references-of", metavar="ID", help="only works this OpenAlex id cites")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--from-year", type=int)
    p.add_argument("--to-year", type=int)
    p.add_argument("--sort", help="e.g. cited_by_count:desc (default: relevance, or citations when there is no query)")
    p.add_argument("--full", action="store_true", help="emit JSON including abstracts")
    p.set_defaults(func=cmd_search)

    paper = sub.add_parser("paper", help="inspect or extend the paper cache")
    paper_sub = paper.add_subparsers(dest="paper_command", required=True)
    p = paper_sub.add_parser("show", help="print a cached paper, abstract included")
    p.add_argument("id")
    p.add_argument("--text", action="store_true", help="print the attached full text instead of the record")
    p.set_defaults(func=cmd_paper_show)
    p = paper_sub.add_parser("get", help="cache a paper from OpenAlex by DOI, arXiv id or URL, PubMed id or OpenAlex id")
    p.add_argument("identifiers", nargs="+")
    p.set_defaults(func=cmd_paper_get)

    p = paper_sub.add_parser("reid", help="give a hand-added paper its OpenAlex record, in every subgraph and lead that cites it")
    p.add_argument("old", help="the id it was added under, e.g. arxiv:2201.11903")
    p.add_argument("identifier", nargs="?", help="DOI, arXiv id or OpenAlex id to look up (default: the old id)")
    p.add_argument("--force", action="store_true", help="accept a record whose title differs from the one held")
    p.set_defaults(func=cmd_paper_reid)

    p = paper_sub.add_parser("add", help="cache a paper from a JSON file (non-OpenAlex sources)")
    p.add_argument("file")
    p.set_defaults(func=cmd_paper_add)
    p = paper_sub.add_parser("text", help="attach full text, so passages can be verified")
    p.add_argument("id")
    p.add_argument("file")
    p.set_defaults(func=cmd_paper_text)

    p = sub.add_parser("fetch", help="find an open copy of each paper and attach its full text")
    p.add_argument("ids", nargs="*")
    p.add_argument("--subgraph", help="every paper this subgraph cites")
    p.add_argument("--force", action="store_true", help="replace full text that is already attached")
    p.set_defaults(func=cmd_fetch)

    p = sub.add_parser("new", help="start a subgraph for a research question")
    p.add_argument("slug")
    p.add_argument("--question", help="defaults to the lead's follow-up when --lead is given")
    p.add_argument("--lead", help="id of the lead this subgraph follows up; seeds its claim as a derived hypothesis")
    p.add_argument("--model", help="the language model doing this work (the tool cannot see it)")
    _framing_arguments(p)
    p.set_defaults(func=cmd_new)

    p = sub.add_parser("frame", help="add to how an existing subgraph's question is framed")
    p.add_argument("slug")
    _framing_arguments(p)
    p.set_defaults(func=cmd_frame)

    p = sub.add_parser("add", help="validate a batch of nodes and edges and apply it")
    p.add_argument("slug")
    p.add_argument("batch", help="JSON file, or - for stdin")
    p.add_argument("--model", help="the language model doing this work (the tool cannot see it)")
    p.set_defaults(func=cmd_add)

    p = sub.add_parser("show", help="print a subgraph")
    p.add_argument("slug")
    p.add_argument("--format", choices=["summary", "json", "mermaid", "markdown"], default="summary")
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("lint", help="structural checks: what to look at again")
    p.add_argument("slug")
    p.set_defaults(func=cmd_lint)

    p = sub.add_parser("duplicates", help="conditions or hypotheses within a subgraph that may be one thing under two ids")
    p.add_argument("slug")
    p.add_argument("--min-score", type=float, default=0.5)
    p.add_argument("--lexical", action="store_true", help="word overlap only, even if embeddings are installed")
    p.add_argument("--format", choices=["json", "markdown"], default="json")
    p.set_defaults(func=cmd_duplicates)

    p = sub.add_parser("merge", help="fold one node into another: the same thing recorded under two ids")
    p.add_argument("slug")
    p.add_argument("keep")
    p.add_argument("drop")
    p.add_argument("--model", help="the language model doing this work (the tool cannot see it)")
    p.set_defaults(func=cmd_merge)

    p = sub.add_parser("retract", help="take a wrong node or edge out of a subgraph, keeping a record of what it was and why")
    p.add_argument("slug")
    what = p.add_mutually_exclusive_group(required=True)
    what.add_argument("--edge", nargs=3, metavar=("SOURCE", "RELATION", "TARGET"))
    what.add_argument("--node", help="the node and every edge that touches it")
    p.add_argument("--paper", help="which paper's edge, when several papers evidence the same relation")
    p.add_argument("--reason", required=True, help="what was wrong with it")
    p.add_argument("--model", help="the language model doing this work (the tool cannot see it)")
    p.set_defaults(func=cmd_retract)

    vo = sub.add_parser("voice", help="exemplar papers whose voice a write-up should take")
    vo_sub = vo.add_subparsers(dest="voice_command", required=True)
    p = vo_sub.add_parser("set", help="add an exemplar from a DOI, arXiv id or OpenAlex id with an open copy, or from a file")
    p.add_argument("source", help="an identifier, or a .pdf, .txt, .tex, .md or .html file")
    p.add_argument("--name", help="what to call it (default: from the title)")
    p.add_argument("--default", action="store_true", help="use it when a write-up names no voice")
    p.set_defaults(func=cmd_voice_set)
    p = vo_sub.add_parser("list", help="the exemplars held")
    p.set_defaults(func=cmd_voice_list)
    p = vo_sub.add_parser("show", help="one exemplar and where its text is (the default one if none is named)")
    p.add_argument("name", nargs="?")
    p.set_defaults(func=cmd_voice_show)

    dr = sub.add_parser("draft", help="a write-up of pooled subgraphs as a paper")
    dr_sub = dr.add_subparsers(dest="draft_command", required=True)
    p = dr_sub.add_parser("start", help="make (or refresh) a draft directory: the dossier, refs.bib and a LaTeX skeleton arXiv accepts")
    p.add_argument("dir")
    p.add_argument("slugs", nargs="+", help="the pooled subgraphs the paper rests on")
    p.add_argument("--focus", help="the steer: what the paper is about. Orders the dossier; leaves nothing out")
    p.add_argument("--voice", help="which exemplar's voice to take (default: the default exemplar)")
    p.set_defaults(func=cmd_draft_start)
    p = dr_sub.add_parser("cite", help="add a reference by DOI, arXiv id or paper id, and print its citation key")
    p.add_argument("dir")
    p.add_argument("identifiers", nargs="+")
    p.set_defaults(func=cmd_draft_cite)
    p = dr_sub.add_parser("check", help="citations resolve, quotations are verbatim, arXiv's requirements hold, prose tells, and it compiles")
    p.add_argument("dir")
    p.add_argument("--no-compile", action="store_true")
    p.set_defaults(func=cmd_draft_check)

    p = sub.add_parser("list", help="list local subgraphs")
    p.add_argument("--format", choices=["text", "markdown"], default="text")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("bibtex", help="BibTeX for the papers cited by subgraphs (all local ones by default)")
    p.add_argument("slugs", nargs="*")
    p.add_argument("-o", "--output", help="write a .bib file instead of printing")
    p.set_defaults(func=cmd_bibtex)

    bd = sub.add_parser("builder", help="who built a subgraph")
    bd_sub = bd.add_subparsers(dest="builder_command", required=True)
    p = bd_sub.add_parser("add", help="record a builder on an existing subgraph")
    p.add_argument("slug")
    for flag in ("--person", "--agent", "--model", "--session"):
        p.add_argument(flag)
    p.set_defaults(func=cmd_builder_add)

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
    p.add_argument("--model", help="the language model doing this work (the tool cannot see it)")
    p.set_defaults(func=cmd_align_add)
    p = al_sub.add_parser("list", help="print the pool's alignment verdicts")
    p.add_argument("--format", choices=["text", "markdown"], default="text")
    p.set_defaults(func=cmd_align_list)

    ld = sub.add_parser("lead", help="record or list leads drawn from the pool")
    ld_sub = ld.add_subparsers(dest="lead_command", required=True)
    p = ld_sub.add_parser("add", help="validate a JSON list of leads and store it in the pool")
    p.add_argument("file", help="JSON file, or - for stdin")
    p.add_argument("--model", help="the language model doing this work (the tool cannot see it)")
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
