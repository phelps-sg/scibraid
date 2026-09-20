"""Literature retrieval from OpenAlex (no key required, but see `OpenAlexError`)."""

from __future__ import annotations

import os
import re

import httpx

from .models import Paper, SourceTier

API = "https://api.openalex.org/works"
FIELDS = (
    "id,doi,title,publication_year,authorships,primary_location,"
    "cited_by_count,type,abstract_inverted_index,open_access,best_oa_location,locations,ids"
)
GREY_TYPES = {"preprint", "dissertation", "report", "other", "posted-content"}
ARXIV_ID = re.compile(r"(?<![\d.])(\d{4}\.\d{4,5})(?:v\d+)?(?![\d])")
_ARXIV_URL = re.compile(r"arxiv\.org/(?:abs|pdf|html)/(\d{4}\.\d{4,5})", re.I)


class OpenAlexError(Exception):
    """A request OpenAlex refused, said in a way the reader can act on."""


def _abstract(inverted: dict[str, list[int]] | None) -> str | None:
    if not inverted:
        return None
    words = sorted((pos, word) for word, positions in inverted.items() for pos in positions)
    return " ".join(word for _, word in words)


def _arxiv_id(work: dict) -> str | None:
    doi = (work.get("doi") or "").lower()
    if "10.48550/arxiv." in doi:
        return doi.split("10.48550/arxiv.", 1)[1]
    for location in work.get("locations") or []:
        for field in ("landing_page_url", "pdf_url"):
            if found := _ARXIV_URL.search(location.get(field) or ""):
                return found.group(1)
    return None


def _paper(work: dict) -> Paper:
    location = work.get("primary_location") or {}
    source = location.get("source") or {}
    access = work.get("open_access") or {}
    best = work.get("best_oa_location") or {}
    pmcid = ((work.get("ids") or {}).get("pmcid") or "").rstrip("/").rsplit("/", 1)[-1] or None
    return Paper(
        id=work["id"].rsplit("/", 1)[-1],
        title=work.get("title") or "(untitled)",
        doi=(work.get("doi") or "").removeprefix("https://doi.org/") or None,
        year=work.get("publication_year"),
        authors=[a["author"]["display_name"] for a in work.get("authorships", [])[:8]],
        venue=source.get("display_name"),
        url=location.get("landing_page_url") or work.get("doi"),
        cited_by_count=work.get("cited_by_count"),
        source_tier=SourceTier.GREY if work.get("type") in GREY_TYPES else SourceTier.PUBLISHED,
        abstract=_abstract(work.get("abstract_inverted_index")),
        oa_status=access.get("oa_status"),
        oa_url=best.get("pdf_url") or access.get("oa_url") or best.get("landing_page_url"),
        arxiv_id=_arxiv_id(work),
        pmcid=pmcid,
    )


def _get(url: str, params: dict[str, str], client: httpx.Client | None) -> dict:
    # Both optional: OpenAlex's polite pool, and a key with its own budget.
    if mailto := os.environ.get("SCIBRAID_MAILTO"):
        params["mailto"] = mailto
    if key := os.environ.get("OPENALEX_API_KEY"):
        params["api_key"] = key
    response = (client or httpx).get(url, params=params, timeout=30)
    if response.status_code == 429:
        raise OpenAlexError(
            "OpenAlex refused the request: the daily budget is spent. Without a key, requests share one "
            "free budget per IP address, which resets at midnight UTC. A key is free and has its own "
            "budget (https://help.openalex.org/api/authentication/): set OPENALEX_API_KEY."
        )
    if response.status_code == 404:
        raise OpenAlexError("OpenAlex has no such work")
    response.raise_for_status()
    return response.json()


def search(
    query: str | None = None,
    limit: int = 20,
    from_year: int | None = None,
    to_year: int | None = None,
    sort: str | None = None,
    client: httpx.Client | None = None,
    match: str = "title-abstract",
    citing: str | None = None,
    references_of: str | None = None,
) -> list[Paper]:
    """Search works, optionally among those citing a paper or among a paper's references.

    OpenAlex's own `search` also matches full text, which it holds only for open-access works, so
    it ranks those above closed ones. Matching on title and abstract treats both alike. Papers
    without an abstract are kept: leaving them out loses closed papers far more often than open ones.
    """
    filters = []
    params = {"per-page": str(min(limit, 100)), "select": FIELDS}
    if query and match == "anywhere":
        params["search"] = query
    elif query:
        filters.append("title_and_abstract.search:" + re.sub(r"[,|]", " ", query))
    if citing:
        filters.append(f"cites:{citing}")
    if references_of:
        filters.append(f"cited_by:{references_of}")
    if from_year:
        filters.append(f"from_publication_date:{from_year}-01-01")
    if to_year:
        filters.append(f"to_publication_date:{to_year}-12-31")
    if not query and not filters:
        raise OpenAlexError("give a query, or a paper whose references or citing works to list")
    if filters:
        params["filter"] = ",".join(filters)
    params["sort"] = sort or ("relevance_score:desc" if query else "cited_by_count:desc")
    return [_paper(work) for work in _get(API, params, client)["results"]]


def work_path(identifier: str) -> str:
    """The OpenAlex path for a DOI, an arXiv id or URL, a PubMed id, or an OpenAlex id."""
    text = identifier.strip()
    if found := re.search(r"(?:^|openalex\.org/)(W\d+)$", text, re.I):
        return found.group(1).upper()
    if found := re.search(r"\b(10\.\d{4,9}/\S+)", text):
        return f"doi:{found.group(1).rstrip('.,;')}"
    if found := re.match(r"(?i)pmid:\s*(\d+)$", text):
        return f"pmid:{found.group(1)}"
    if found := re.match(r"(?i)(?:pmcid:\s*)?(PMC\d+)$", text):
        return f"pmcid:{found.group(1).upper()}"
    if found := _ARXIV_URL.search(text) or ARXIV_ID.search(text):
        return f"doi:10.48550/arXiv.{found.group(1)}"
    raise OpenAlexError(f"{identifier!r} is not a DOI, an arXiv id, a PubMed id or an OpenAlex id")


def get(identifier: str, client: httpx.Client | None = None) -> Paper:
    return _paper(_get(f"{API}/{work_path(identifier)}", {"select": FIELDS}, client))
