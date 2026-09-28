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
    if paper.arxiv_id:
        return paper.arxiv_id
    match = _ARXIV.search(f"{paper.id} {paper.url or ''} {paper.doi or ''}")
    return match.group(1) if match else None


# Places a preprint is deposited. OpenAlex gives them as a work's venue, and a reference list that
# prints one as a journal ("RePEc: Research Papers in Economics, 2024") reads as a mistake.
_REPOSITORIES = ("arxiv", "repec", "ssrn", "osf", "psyarxiv", "socarxiv", "biorxiv", "medrxiv", "research square", "zenodo", "preprints")
_PROCEEDINGS = ("proceedings", "conference", "workshop", "symposium", "findings of", "advances in neural")


# The ACL Anthology's DOIs name the volume, which OpenAlex often does not.
_ACL = re.compile(r"10\.18653/v1/(\d{4})\.([a-z]+)-([a-z]+)\.", re.I)
_ACL_EVENTS = {"acl": "ACL", "emnlp": "EMNLP", "naacl": "NAACL", "eacl": "EACL", "coling": "COLING"}


def venue(paper: Paper) -> str | None:
    """The journal or proceedings a paper appeared in, or None if what is recorded is a repository."""
    name = (paper.venue or "").strip()
    if name and not any(r in name.lower() for r in _REPOSITORIES):
        return name
    if found := _ACL.search(paper.doi or ""):
        year, first, second = found.groups()
        if first.lower() == "findings" and second.lower() in _ACL_EVENTS:
            return f"Findings of the Association for Computational Linguistics: {_ACL_EVENTS[second.lower()]} {year}"
        if first.lower() in _ACL_EVENTS:
            return f"Proceedings of {_ACL_EVENTS[first.lower()]} {year}"
    return None


_PARTICLES = {"van", "von", "de", "der", "den", "del", "della", "di", "da", "la", "le", "ten", "ter"}


def _name(name: str) -> str:
    """BibTeX reads a capitalised particle as a forename ("Katherine Van Koevering" files under K),
    so a surname that starts with one is given whole, in braces."""
    parts = name.split()
    for i in range(1, len(parts) - 1):
        if parts[i].lower() in _PARTICLES and parts[i][0].isupper():
            return "{" + " ".join(parts[i:]) + "}, " + " ".join(parts[:i])
    return name


def entry(paper: Paper, key: str | None = None) -> str:
    eprint, where = arxiv_id(paper), venue(paper)
    authors = " and ".join(_name(a) for a in paper.authors) + (" and others" if len(paper.authors) >= AUTHOR_CAP else "")
    fields = [("title", "{" + _escape(paper.title) + "}"), ("author", _escape(authors) if authors else None), ("year", str(paper.year) if paper.year else None)]
    if where:
        proceedings = any(word in where.lower() for word in _PROCEEDINGS)
        kind = "inproceedings" if proceedings else "article"
        fields += [("booktitle" if proceedings else "journal", _escape(where)), ("volume", paper.volume), ("number", paper.issue), ("pages", paper.pages)]
    else:
        kind = "misc"
        if eprint:
            fields += [("note", f"arXiv preprint arXiv:{eprint}"), ("eprint", eprint), ("archivePrefix", "arXiv")]
    # A DOI or an arXiv number already says where the paper is; a URL beside it is noise.
    own_doi = paper.doi if paper.doi and not (eprint and "arxiv" in paper.doi.lower()) else None
    if own_doi and where:
        fields.append(("doi", own_doi))
    elif own_doi and not eprint:
        fields.append(("url", f"https://doi.org/{own_doi}"))  # the styles print no DOI for an unpublished work
    elif not eprint and not (where and paper.volume):
        fields.append(("url", paper.url))
    body = ",\n".join(f"  {name} = {{{value}}}" for name, value in fields if value)
    return f"@{kind}{{{key or cite_key(paper)},\n{body}\n}}"


def bibliography(papers: list[Paper]) -> dict[str, dict[str, str]]:
    """paper id -> {key, entry}, with keys made unique by a letter suffix."""
    out, used = {}, {}
    for paper in sorted(papers, key=lambda p: (p.year or 0, p.title)):
        base = cite_key(paper)
        used[base] = used.get(base, 0) + 1
        key = base if used[base] == 1 else base + chr(ord("a") + used[base] - 2)
        out[paper.id] = {"key": key, "entry": entry(paper, key)}
    return out
