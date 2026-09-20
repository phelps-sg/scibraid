"""Pooling: where independently built subgraphs accumulate.

The pool is local SQLite for now. Setting SCIGRAPH_POOL_URL switches to a remote
pool over HTTP; the contract is `POST {url}/subgraphs` with the subgraph JSON and
`GET {url}/subgraphs` for the listing, so the skill does not change when a server
arrives. Pooled subgraphs are kept separate (node ids are namespaced by slug):
alignment adds links between them, it never merges them destructively.
"""

from __future__ import annotations

import json
import os
import sqlite3
from typing import Protocol

import httpx

from .models import Subgraph, _now
from .store import home

SCHEMA = """
CREATE TABLE IF NOT EXISTS subgraphs (
    slug TEXT PRIMARY KEY, question TEXT NOT NULL, pushed TEXT NOT NULL, payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS nodes (
    id TEXT PRIMARY KEY, slug TEXT NOT NULL REFERENCES subgraphs ON DELETE CASCADE,
    type TEXT NOT NULL, label TEXT NOT NULL, description TEXT NOT NULL, outcome TEXT
);
CREATE TABLE IF NOT EXISTS edges (
    slug TEXT NOT NULL REFERENCES subgraphs ON DELETE CASCADE,
    source TEXT NOT NULL, target TEXT NOT NULL, relation TEXT NOT NULL,
    confidence REAL NOT NULL, asserted_by TEXT NOT NULL, paper_id TEXT NOT NULL
);
"""


class Pool(Protocol):
    def push(self, sg: Subgraph) -> str: ...
    def listing(self) -> list[dict]: ...


class LocalPool:
    def __init__(self) -> None:
        self.path = home() / "pool.sqlite"

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path)
        db.execute("PRAGMA foreign_keys = ON")
        db.executescript(SCHEMA)
        return db

    def push(self, sg: Subgraph) -> str:
        with self._connect() as db:
            db.execute("DELETE FROM subgraphs WHERE slug = ?", (sg.slug,))
            db.execute(
                "INSERT INTO subgraphs VALUES (?, ?, ?, ?)",
                (sg.slug, sg.question, _now(), sg.model_dump_json()),
            )
            db.executemany(
                "INSERT INTO nodes VALUES (?, ?, ?, ?, ?, ?)",
                [
                    (f"{sg.slug}/{n.id}", sg.slug, n.type, n.label, n.description, n.outcome)
                    for n in sg.nodes.values()
                ],
            )
            db.executemany(
                "INSERT INTO edges VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        sg.slug,
                        f"{sg.slug}/{e.source}",
                        f"{sg.slug}/{e.target}",
                        e.relation,
                        e.confidence,
                        e.asserted_by,
                        e.provenance[0].paper_id,
                    )
                    for e in sg.edges
                ],
            )
        return str(self.path)

    def listing(self) -> list[dict]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT s.slug, s.question, s.pushed,"
                " (SELECT COUNT(*) FROM nodes n WHERE n.slug = s.slug),"
                " (SELECT COUNT(*) FROM edges e WHERE e.slug = s.slug)"
                " FROM subgraphs s ORDER BY s.pushed"
            ).fetchall()
        keys = ("slug", "question", "pushed", "nodes", "edges")
        return [dict(zip(keys, row)) for row in rows]


class HttpPool:
    def __init__(self, url: str, client: httpx.Client | None = None) -> None:
        self.url = url.rstrip("/")
        self.client = client or httpx.Client(timeout=60)

    def push(self, sg: Subgraph) -> str:
        response = self.client.post(f"{self.url}/subgraphs", json=json.loads(sg.model_dump_json()))
        response.raise_for_status()
        return f"{self.url}/subgraphs/{sg.slug}"

    def listing(self) -> list[dict]:
        response = self.client.get(f"{self.url}/subgraphs")
        response.raise_for_status()
        return response.json()


def get_pool() -> Pool:
    url = os.environ.get("SCIGRAPH_POOL_URL")
    return HttpPool(url) if url else LocalPool()
