"""Optional sentence embeddings for the candidate stage.

Installed with the `embeddings` extra (fastembed: ONNX, no PyTorch, no API key).
Without it everything still works on lexical similarity alone. Measured on three
pooled subgraphs, embeddings find condition matches that share no words ("models
well below 100B" ~ "open models spanning 500M to 70B") but also rank unrelated
pairs highly, so they are only ever used together with the lexical score.
"""

from __future__ import annotations

import hashlib
import sqlite3
import struct
from typing import Protocol

from .store import home

MODEL = "BAAI/bge-small-en-v1.5"
# Cosine for this model runs from about 0.5 (unrelated) to 1.0. Judged `same` pairs
# averaged 0.91 and `different` pairs 0.67, so similarity below 0.70 counts for nothing.
FLOOR, SPAN = 0.70, 0.25


class Embedder(Protocol):
    def vectors(self, texts: list[str]) -> list[list[float]]: ...


def calibrated(cosine: float) -> float:
    """Map raw cosine onto 0..1 so it can be averaged with the lexical score."""
    return min(1.0, max(0.0, (cosine - FLOOR) / SPAN))


def cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))  # vectors are stored normalised


class FastEmbedder:
    """bge-small via fastembed, with vectors cached on disk by text hash."""

    def __init__(self) -> None:
        from fastembed import TextEmbedding  # ImportError means the extra is not installed

        self._model = TextEmbedding(MODEL)
        self._db = sqlite3.connect(home() / "embeddings.sqlite")
        self._db.execute("CREATE TABLE IF NOT EXISTS vectors (key TEXT PRIMARY KEY, dim INTEGER, data BLOB)")

    @staticmethod
    def _key(text: str) -> str:
        return hashlib.sha256(f"{MODEL}\n{text}".encode()).hexdigest()

    def vectors(self, texts: list[str]) -> list[list[float]]:
        found: dict[str, list[float]] = {}
        for text in set(texts):
            row = self._db.execute("SELECT dim, data FROM vectors WHERE key = ?", (self._key(text),)).fetchone()
            if row:
                found[text] = list(struct.unpack(f"{row[0]}f", row[1]))
        missing = [t for t in dict.fromkeys(texts) if t not in found]
        if missing:
            for text, raw in zip(missing, self._model.embed(missing)):
                length = sum(float(x) ** 2 for x in raw) ** 0.5 or 1.0
                vector = [float(x) / length for x in raw]
                found[text] = vector
                blob = struct.pack(f"{len(vector)}f", *vector)
                self._db.execute("INSERT OR REPLACE INTO vectors VALUES (?, ?, ?)", (self._key(text), len(vector), blob))
            self._db.commit()
        return [found[t] for t in texts]


def default_embedder() -> Embedder | None:
    try:
        return FastEmbedder()
    except ImportError:
        return None
