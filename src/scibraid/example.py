"""The bundled example: three reviews, the judgements aligning them, and the leads checked on them.

It is the README's worked example (emergent abilities, and chain-of-thought and scale) plus the
review that one of its leads prompted, kept as JSON so that a new user can browse a finished pool,
and run `observe`, `lead list` and `agenda` on it, before spending anything on a review of their
own. It carries no full text: each subgraph holds its papers' metadata and abstracts and the
passages it quotes.

`scripts/export_example.py` regenerates the data from a working data directory.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path

from . import store
from .models import Alignment, Lead, Subgraph
from .pool import LocalPool

DATA = Path(__file__).with_name("example_data")
MARKER = ".scibraid-example"


def install(dest: Path) -> None:
    """Write the example into `dest` as a data directory of its own."""
    dest.mkdir(parents=True, exist_ok=True)
    with using(dest):
        pool = LocalPool()
        for path in sorted((DATA / "subgraphs").glob("*.json")):
            sg = Subgraph.model_validate_json(path.read_text())
            store.save_subgraph(sg)
            pool.push(sg)
        pool.add_alignments([Alignment.model_validate(x) for x in _load("alignments.json")])
        pool.add_leads([Lead.model_validate(x) for x in _load("leads.json")])
    (dest / MARKER).write_text("written by `scibraid example`; safe to delete\n")


def _load(name: str) -> list[dict]:
    import json

    return json.loads((DATA / name).read_text())


@contextmanager
def using(home: Path):
    """Point the tool at `home`, and at a local pool, for the duration."""
    saved = {k: os.environ.get(k) for k in ("SCIBRAID_HOME", "SCIBRAID_POOL_URL")}
    os.environ["SCIBRAID_HOME"] = str(home)
    os.environ.pop("SCIBRAID_POOL_URL", None)
    try:
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def refusal(dest: Path) -> str | None:
    """Why `dest` should not be written to, if it should not be: it holds something else."""
    if dest.exists() and any(dest.iterdir()) and not (dest / MARKER).exists():
        return f"{dest} exists and was not made by `scibraid example`; choose another directory"
    return None
