"""Regenerate the bundled example from a working data directory.

    uv run python scripts/export_example.py

Exports the README's two subgraphs and the follow-up review that one of their leads prompted, the
alignment judgements among them, and the leads that rest on nothing else. Refuses to write anything
that mentions another subgraph, so the example stays self-contained.
"""

import json
from pathlib import Path

from scibraid import store
from scibraid.pool import LocalPool

SLUGS = ("llm-emergent-abilities", "cot-scale-threshold", "post-training-and-cot")
OUT = Path(__file__).resolve().parent.parent / "src" / "scibraid" / "example_data"


def slug_of(key: str) -> str:
    return key.split("/")[0]


def main() -> None:
    pool = LocalPool()
    others = [sg.slug for sg in pool.subgraphs() if sg.slug not in SLUGS]

    (OUT / "subgraphs").mkdir(parents=True, exist_ok=True)
    for slug in SLUGS:
        sg = store.load_subgraph(slug)
        (OUT / "subgraphs" / f"{slug}.json").write_text(sg.model_dump_json(indent=2) + "\n")

    alignments = [x for x in pool.alignments() if slug_of(x.a) in SLUGS and slug_of(x.b) in SLUGS]
    leads = [
        x
        for x in pool.leads()
        if all(slug_of(n) in SLUGS for n in x.nodes) and all(slug_of(a) in SLUGS and slug_of(b) in SLUGS for a, b in x.alignments)
    ]
    for name, items in (("alignments.json", alignments), ("leads.json", leads)):
        text = json.dumps([json.loads(x.model_dump_json()) for x in items], indent=2, ensure_ascii=False) + "\n"
        strays = [s for s in others if s in text]
        if strays:
            raise SystemExit(f"{name} mentions other subgraphs: {strays}")
        (OUT / name).write_text(text)
    print(f"{len(SLUGS)} subgraphs, {len(alignments)} alignments, {len(leads)} leads -> {OUT}")


if __name__ == "__main__":
    main()
