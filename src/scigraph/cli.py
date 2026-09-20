"""scigraph: deterministic tools for building and pooling evidence subgraphs.

The semantic work (reading, extracting, judging) is done by the agent driving
these commands; nothing here calls a model.
"""

from __future__ import annotations

import argparse
import json
import sys
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from collections import Counter
from pathlib import Path

import httpx
from pydantic import ValidationError

from . import openalex, store
from .lint import lint
from .models import Batch, Paper, Subgraph
from .pool import get_pool


def _emit(data: object) -> None:
    print(json.dumps(data, indent=2, ensure_ascii=False))


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
    print(f"\n{len(papers)} papers cached; read one with `scigraph paper show <id>`")
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
    if store.subgraph_path(args.slug).exists():
        print(f"subgraph {args.slug!r} already exists", file=sys.stderr)
        return 1
    try:
        sg = Subgraph(slug=args.slug, question=args.question)
    except ValidationError as exc:
        _emit({"ok": False, "errors": _errors(exc)})
        return 1
    store.save_subgraph(sg)
    print(store.subgraph_path(sg.slug))
    return 0


def cmd_add(args: argparse.Namespace) -> int:
    sg = store.load_subgraph(args.slug)
    raw = sys.stdin.read() if args.batch == "-" else Path(args.batch).read_text()
    try:
        batch = Batch.model_validate_json(raw)
    except ValidationError as exc:
        _emit({"ok": False, "errors": _errors(exc)})
        return 1
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
    for sg in store.list_subgraphs():
        print(f"{sg.slug}  {_totals(sg)}  {sg.question}")
    return 0


def render_view(subgraphs: list[Subgraph]) -> str:
    """A self-contained HTML page for browsing the given subgraphs."""
    data = json.dumps({"subgraphs": [sg.model_dump(mode="json") for sg in subgraphs]}, ensure_ascii=False)
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
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="scigraph", description=__doc__.splitlines()[0])
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
    p.add_argument("--question", required=True)
    p.set_defaults(func=cmd_new)

    p = sub.add_parser("add", help="validate a batch of nodes and edges and apply it")
    p.add_argument("slug")
    p.add_argument("batch", help="JSON file, or - for stdin")
    p.set_defaults(func=cmd_add)

    p = sub.add_parser("show", help="print a subgraph")
    p.add_argument("slug")
    p.add_argument("--format", choices=["summary", "json", "mermaid"], default="summary")
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("lint", help="structural checks: what to look at again")
    p.add_argument("slug")
    p.set_defaults(func=cmd_lint)

    p = sub.add_parser("list", help="list local subgraphs")
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
    p.set_defaults(func=cmd_pool)
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
