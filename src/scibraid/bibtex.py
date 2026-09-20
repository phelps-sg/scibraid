"""BibTeX for the papers a subgraph cites, generated from the cached metadata."""

from __future__ import annotations

import re
import unicodedata

from .models import Paper, surname as family_name

_STOP = {"a", "an", "the", "on", "of", "in", "for", "and", "to", "is", "are", "do", "does", "can", "how", "why", "what", "with", "from"}
_ARXIV = re.compile(r"(?:arxiv[:.]|arxiv\.org/abs/)(\d{4}\.\d{4,5})", re.I)
# OpenAlex results are stored with at most this many authors; a full list that long was probably cut.
AUTHOR_CAP = 8


def _ascii(text: str) -> str:
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()


def _escape(text: str) -> str:
    return re.sub(r"([&%$#_])", r"\\\1", text)


def cite_key(paper: Paper) -> str:
    surname = _ascii(family_name(paper.authors[0]) if paper.authors else "anon").lower()
    words = [w for w in re.findall(r"[a-z0-9]+", _ascii(paper.title).lower()) if w not in _STOP]
    return re.sub(r"[^a-z0-9]", "", surname) + str(paper.year or "") + (words[0] if words else "")


def arxiv_id(paper: Paper) -> str | None:
    match = _ARXIV.search(f"{paper.id} {paper.url or ''} {paper.doi or ''}")
    return match.group(1) if match else None


def entry(paper: Paper, key: str | None = None) -> str:
    eprint = arxiv_id(paper)
    preprint = eprint is not None and not (paper.venue and "arxiv" not in paper.venue.lower())
    authors = " and ".join(paper.authors) + (" and others" if len(paper.authors) >= AUTHOR_CAP else "")
    fields = [("title", "{" + _escape(paper.title) + "}"), ("author", _escape(authors) if authors else None), ("year", str(paper.year) if paper.year else None)]
    if paper.venue and not preprint:
        fields.append(("journal", _escape(paper.venue)))
    if eprint:
        fields += [("eprint", eprint), ("archivePrefix", "arXiv")]
    if paper.doi:
        fields.append(("doi", paper.doi))
    fields.append(("url", paper.url or (f"https://doi.org/{paper.doi}" if paper.doi else None)))
    body = ",\n".join(f"  {name} = {{{value}}}" for name, value in fields if value)
    return f"@{'misc' if preprint or not paper.venue else 'article'}{{{key or cite_key(paper)},\n{body}\n}}"


def bibliography(papers: list[Paper]) -> dict[str, dict[str, str]]:
    """paper id -> {key, entry}, with keys made unique by a letter suffix."""
    out, used = {}, {}
    for paper in sorted(papers, key=lambda p: (p.year or 0, p.title)):
        base = cite_key(paper)
        used[base] = used.get(base, 0) + 1
        key = base if used[base] == 1 else base + chr(ord("a") + used[base] - 2)
        out[paper.id] = {"key": key, "entry": entry(paper, key)}
    return out
