"""Literature retrieval from OpenAlex (no key required)."""

from __future__ import annotations

import os

import httpx

from .models import Paper, SourceTier

API = "https://api.openalex.org/works"
FIELDS = (
    "id,doi,title,publication_year,authorships,primary_location,"
    "cited_by_count,type,abstract_inverted_index"
)
GREY_TYPES = {"preprint", "dissertation", "report", "other", "posted-content"}


def _abstract(inverted: dict[str, list[int]] | None) -> str | None:
    if not inverted:
        return None
    words = sorted((pos, word) for word, positions in inverted.items() for pos in positions)
    return " ".join(word for _, word in words)


def _paper(work: dict) -> Paper:
    location = work.get("primary_location") or {}
    source = location.get("source") or {}
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
    )


def search(
    query: str,
    limit: int = 20,
    from_year: int | None = None,
    to_year: int | None = None,
    sort: str = "relevance_score:desc",
    client: httpx.Client | None = None,
) -> list[Paper]:
    filters = ["has_abstract:true"]
    if from_year:
        filters.append(f"from_publication_date:{from_year}-01-01")
    if to_year:
        filters.append(f"to_publication_date:{to_year}-12-31")
    params = {
        "search": query,
        "per-page": str(min(limit, 100)),
        "select": FIELDS,
        "filter": ",".join(filters),
        "sort": sort,
    }
    # Both optional: OpenAlex's polite pool, and a key for higher rate limits.
    if mailto := os.environ.get("SCIBRAID_MAILTO"):
        params["mailto"] = mailto
    if key := os.environ.get("OPENALEX_API_KEY"):
        params["api_key"] = key
    response = (client or httpx).get(API, params=params, timeout=30)
    response.raise_for_status()
    return [_paper(work) for work in response.json()["results"]]
