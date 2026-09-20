"""Fetch an open copy of a paper and turn it into plain text that passages can be checked against.

Sources are tried from the most structured to the least: arXiv's HTML, Europe PMC's XML, then
PDFs. Structured sources keep section headings (as `## ` lines) and inline mathematics (as the
LaTeX the source carries); a PDF keeps neither reliably.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path

import httpx

from .models import Paper
from .openalex import ARXIV_ID

HEADERS = {"User-Agent": "scibraid/0.1 (literature review tool)"}
_SKIP = {"script", "style", "annotation", "annotation-xml", "nav", "header", "footer", "button", "noscript"}
_BLOCK = {"p", "div", "li", "br", "tr", "section", "figcaption", "blockquote", "table", "article"}
_HEADING = {"h1", "h2", "h3", "h4", "h5", "h6"}


class FetchError(Exception):
    pass


@dataclass
class Fetched:
    text: str
    source: str
    url: str


def _tidy(text: str) -> str:
    text = re.sub(r"[ \t\xa0]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


def _plain(latex: str) -> str:
    """LaTeX for a plain quantity reads as the quantity: `40\\%` is 40%, `\\sim 10` stays as it is."""
    return re.sub(r"\\([%$&#_])", r"\1", latex).replace("\\,", " ")


_META = re.compile(r"<meta\b[^>]*>", re.I)


def _pdf_link(html: str) -> str | None:
    """The `citation_pdf_url` meta tag, whose attributes come in any order and may be unquoted."""
    for tag in _META.findall(html):
        if re.search(r"""name=["']?citation_pdf_url""", tag, re.I) and (found := re.search(r"""content=["']?([^"'\s>]+)""", tag, re.I)):
            return found.group(1)
    return None


class _Html(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.out: list[str] = []
        self.skip = 0
        self.math = 0

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in _SKIP:
            self.skip += 1
        elif tag == "math":
            # The rendered symbols are unreadable as text; the LaTeX the source carries is not.
            self.math += 1
            if not self.skip and (latex := dict(attrs).get("alttext")):
                self.out.append(f" {_plain(latex)} ")
        elif tag in _HEADING:
            self.out.append("\n\n## ")
        elif tag in _BLOCK:
            self.out.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP:
            self.skip = max(0, self.skip - 1)
        elif tag == "math":
            self.math = max(0, self.math - 1)
        elif tag in _HEADING:
            self.out.append("\n\n")

    def handle_data(self, data: str) -> None:
        if not self.skip and not self.math:
            self.out.append(data)


def html_to_text(html: str) -> str:
    parser = _Html()
    parser.feed(html)
    return _tidy("".join(parser.out))


def jats_to_text(xml: str) -> str:
    """Article XML as Europe PMC serves it: abstract and body, section titles kept as headings."""
    root = ET.fromstring(xml)
    out: list[str] = []

    def walk(element: ET.Element) -> None:
        for child in element:
            tag = child.tag.rsplit("}", 1)[-1]
            if tag == "title":
                out.append(f"\n\n## {' '.join(''.join(child.itertext()).split())}\n\n")
            elif tag == "p":
                out.append(" ".join("".join(child.itertext()).split()) + "\n\n")
            elif tag not in {"ref-list", "table-wrap", "fig", "supplementary-material"}:
                walk(child)

    for part in (".//abstract", ".//body"):
        for element in root.iterfind(part):
            walk(element)
    return _tidy("".join(out))


def pdf_to_text(data: bytes) -> str:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "paper.pdf"
        path.write_bytes(data)
        if shutil.which("pdftotext"):
            done = subprocess.run(["pdftotext", "-q", "-nopgbrk", str(path), "-"], capture_output=True, timeout=120)
            text = done.stdout.decode("utf-8", "replace")
        else:
            try:
                from pypdf import PdfReader
            except ImportError as exc:
                raise FetchError("reading a PDF needs `pdftotext` (poppler) on PATH, or the `fulltext` extra") from exc
            text = "\n\n".join(page.extract_text() or "" for page in PdfReader(str(path)).pages)
    # A word broken across a line would otherwise never match a quotation of it.
    return _tidy(re.sub(r"([a-z])-\n([a-z])", r"\1\2", text))


def arxiv_id(paper: Paper) -> str | None:
    if paper.arxiv_id:
        return paper.arxiv_id
    for value in (paper.doi or "", paper.url or "", paper.oa_url or "", paper.id if paper.id.lower().startswith("arxiv") else ""):
        if "arxiv" in value.lower() and (found := ARXIV_ID.search(value)):
            return found.group(1)
    return None


def sources(paper: Paper) -> list[tuple[str, str, str]]:
    """(name, url, format) for each place an open copy might be, best first."""
    found = []
    if arxiv := arxiv_id(paper):
        found.append(("arXiv HTML", f"https://arxiv.org/html/{arxiv}", "html"))
    if paper.pmcid:
        found.append(("Europe PMC", f"https://www.ebi.ac.uk/europepmc/webservices/rest/{paper.pmcid}/fullTextXML", "jats"))
    if arxiv:
        found.append(("arXiv PDF", f"https://arxiv.org/pdf/{arxiv}", "pdf"))
    if paper.oa_url and "arxiv.org" not in paper.oa_url:
        found.append(("open-access copy", paper.oa_url, "pdf" if paper.oa_url.lower().split("?")[0].endswith(".pdf") else "auto"))
    return found


def fetch(paper: Paper, client: httpx.Client | None = None) -> Fetched:
    tried = []
    for name, url, kind in sources(paper):
        try:
            response = (client or httpx).get(url, headers=HEADERS, timeout=60, follow_redirects=True)
            response.raise_for_status()
            if kind == "auto" and response.content[:5] != b"%PDF-":
                # A publisher's landing page is not the paper, but by convention it names the PDF.
                if not (link := _pdf_link(response.text)):
                    raise FetchError("a landing page that names no PDF")
                response = (client or httpx).get(link, headers=HEADERS, timeout=60, follow_redirects=True)
                response.raise_for_status()
                if response.content[:5] != b"%PDF-":
                    raise FetchError("the PDF the landing page names is not a PDF (probably a paywall or a bot check)")
            kind = "pdf" if kind == "auto" else kind
            text = {"html": html_to_text, "jats": jats_to_text}[kind](response.text) if kind != "pdf" else pdf_to_text(response.content)
        except (httpx.HTTPError, ET.ParseError, FetchError, subprocess.SubprocessError) as exc:
            tried.append(f"{name}: {exc}")
            continue
        # A landing page, a paywall notice or a scanned PDF yields little more than the abstract.
        if len(text) < max(6000, 4 * len(paper.abstract or "")):
            tried.append(f"{name}: only {len(text)} characters, which is not a full text")
            continue
        return Fetched(text, name, url)
    if not tried:
        raise FetchError("no open copy is known; attach one by hand with `scibraid paper text`")
    raise FetchError("; ".join(tried))
