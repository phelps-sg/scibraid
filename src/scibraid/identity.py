"""Who is doing the work: the person, the agent harness, the model and the session.

Agreement between two subgraphs only counts as confirmation if they were built
independently, and a lead checked by whoever wrote it has not really been checked. None
of that can be assessed unless each piece of work says who did it. The tool can see the
person, the harness and the session from the environment. It cannot see the model, so
the agent has to say (`--model`, or SCIBRAID_MODEL).
"""

from __future__ import annotations

import os
import subprocess

from .models import Builder


def _git_user() -> str | None:
    try:
        out = subprocess.run(["git", "config", "user.name"], capture_output=True, text=True, timeout=2)
        return out.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def current_builder(model: str | None = None) -> Builder:
    env = os.environ.get
    return Builder(
        person=env("SCIBRAID_PERSON") or _git_user() or env("USER"),
        agent=env("SCIBRAID_AGENT") or ("claude-code" if env("CLAUDECODE") else None),
        model=model or env("SCIBRAID_MODEL"),
        session=env("SCIBRAID_SESSION") or env("CLAUDE_CODE_SESSION_ID"),
    )


# How far apart two pieces of work are, weakest first. The model does the reading, so two
# people using one model share its blind spots; one person using two models does not.
LEVELS = ("unknown", "same reader", "same model", "different model")


def _pair(a: Builder, b: Builder) -> str:
    if a.model is None or b.model is None:
        return "unknown"
    if a.model != b.model:
        return "different model"
    if a.person is None or b.person is None or a.person == b.person:
        return "same reader"
    return "same model"


def relation(first: list[Builder], second: list[Builder]) -> str:
    """The weakest relation between any builder of one and any builder of the other.
    Independence has to be shown, so anything unrecorded counts as unknown."""
    if not first or not second:
        return "unknown"
    return min((_pair(a, b) for a in first for b in second), key=LEVELS.index)


def weakest(levels) -> str:
    levels = list(levels)
    return min(levels, key=LEVELS.index) if levels else "unknown"
