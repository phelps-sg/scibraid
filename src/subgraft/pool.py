"""Pooling: where independently built subgraphs accumulate.

The pool is local SQLite for now. Setting SUBGRAFT_POOL_URL switches to a remote
pool over HTTP; the contract is `POST {url}/subgraphs` with the subgraph JSON and
`GET {url}/subgraphs` for the listing (`?full=1` for whole subgraphs), and
`POST`/`GET {url}/alignments`, so the skills do not change when a server arrives.
Pooled subgraphs are kept separate (node ids are namespaced by slug): alignment
adds links between them, it never merges them destructively.
"""

from __future__ import annotations

import json
import os
import sqlite3
from typing import Protocol

import httpx

from .models import Alignment, Subgraph, _now
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
CREATE TABLE IF NOT EXISTS alignments (
    a TEXT NOT NULL, b TEXT NOT NULL, verdict TEXT NOT NULL, confidence REAL NOT NULL,
    rationale TEXT NOT NULL, judged TEXT NOT NULL, PRIMARY KEY (a, b)
);
"""


class Pool(Protocol):
    def push(self, sg: Subgraph) -> str: ...
    def listing(self) -> list[dict]: ...
    def subgraphs(self) -> list[Subgraph]: ...
    def add_alignments(self, alignments: list[Alignment]) -> None: ...
    def alignments(self) -> list[Alignment]: ...


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

    def subgraphs(self) -> list[Subgraph]:
        with self._connect() as db:
            rows = db.execute("SELECT payload FROM subgraphs ORDER BY pushed").fetchall()
        return [Subgraph.model_validate_json(row[0]) for row in rows]

    def add_alignments(self, alignments: list[Alignment]) -> None:
        with self._connect() as db:
            db.executemany(
                "INSERT OR REPLACE INTO alignments VALUES (?, ?, ?, ?, ?, ?)",
                [(x.a, x.b, x.verdict, x.confidence, x.rationale, x.judged) for x in alignments],
            )

    def alignments(self) -> list[Alignment]:
        with self._connect() as db:
            rows = db.execute("SELECT * FROM alignments ORDER BY judged, a, b").fetchall()
        keys = ("a", "b", "verdict", "confidence", "rationale", "judged")
        return [Alignment(**dict(zip(keys, row))) for row in rows]


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

    def subgraphs(self) -> list[Subgraph]:
        response = self.client.get(f"{self.url}/subgraphs", params={"full": "1"})
        response.raise_for_status()
        return [Subgraph.model_validate(item) for item in response.json()]

    def add_alignments(self, alignments: list[Alignment]) -> None:
        payload = [json.loads(x.model_dump_json()) for x in alignments]
        self.client.post(f"{self.url}/alignments", json=payload).raise_for_status()

    def alignments(self) -> list[Alignment]:
        response = self.client.get(f"{self.url}/alignments")
        response.raise_for_status()
        return [Alignment.model_validate(item) for item in response.json()]


def get_pool() -> Pool:
    url = os.environ.get("SUBGRAFT_POOL_URL")
    return HttpPool(url) if url else LocalPool()
