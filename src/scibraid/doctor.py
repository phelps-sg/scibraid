"""`scibraid doctor`: is this machine set up to use scibraid, and if not, what to do about it.

Each check says how much it matters. A *required* check that fails means the tool cannot work;
a *recommended* one means it works with a handicap; an *optional* one is only needed for one
feature. Only a required failure makes the command exit non-zero.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

import httpx

from . import openalex, store

REQUIRED, RECOMMENDED, OPTIONAL = "required", "recommended", "optional"


@dataclass
class Finding:
    name: str
    ok: bool
    level: str
    detail: str
    fix: str = ""


def _data_directory() -> Finding:
    path = store.home()
    if not os.access(path, os.W_OK):
        return Finding("data directory", False, REQUIRED, f"{path} is not writable", "set SCIBRAID_HOME to a directory you can write to")
    return Finding("data directory", True, REQUIRED, f"{path} ({len(list((path / 'subgraphs').glob('*.json')))} subgraphs)")


def _openalex_key() -> Finding:
    if openalex.api_key():
        return Finding("OpenAlex key", True, RECOMMENDED, "set")
    return Finding(
        "OpenAlex key",
        False,
        RECOMMENDED,
        "none set, so searches share one free daily budget per IP address, which a day of building subgraphs can spend",
        "get a free key (https://help.openalex.org/api/authentication/) and put it in ~/.openalex-tok, or set OPENALEX_API_KEY",
    )


def _openalex_reachable(client: httpx.Client | None = None) -> Finding:
    name = "OpenAlex"
    try:
        openalex._get(openalex.API, {"per-page": "1", "select": "id"}, client)
    except openalex.OpenAlexError as exc:
        return Finding(name, False, REQUIRED, str(exc))
    except httpx.HTTPStatusError as exc:
        code = exc.response.status_code
        if code in (401, 403):
            return Finding(name, False, REQUIRED, f"refused the request ({code}): the key may be wrong", "check ~/.openalex-tok or OPENALEX_API_KEY")
        return Finding(name, False, REQUIRED, f"answered with an error ({code})", "try again later; check https://status.openalex.org")
    except httpx.HTTPError as exc:
        return Finding(name, False, REQUIRED, f"could not be reached ({type(exc).__name__})", "check your network connection, or run with --offline")
    return Finding(name, True, REQUIRED, "reachable")


def _pdf_reader() -> Finding:
    if shutil.which("pdftotext"):
        return Finding("PDF text", True, OPTIONAL, "pdftotext (poppler)")
    if importlib.util.find_spec("pypdf"):
        return Finding("PDF text", True, OPTIONAL, "pypdf")
    return Finding(
        "PDF text",
        False,
        OPTIONAL,
        "no PDF reader, so papers that are only open as PDFs are read from their abstracts",
        "install poppler (`apt install poppler-utils`, `brew install poppler`) or the `fulltext` extra",
    )


def _latex() -> Finding:
    missing = [tool for tool in ("pdflatex", "bibtex") if not shutil.which(tool)]
    if not missing:
        return Finding("LaTeX", True, OPTIONAL, "pdflatex and bibtex")
    return Finding(
        "LaTeX",
        False,
        OPTIONAL,
        f"{' and '.join(missing)} not found, so a write-up is checked but not compiled",
        "install a TeX distribution (`apt install texlive-latex-recommended texlive-bibtex-extra`, `brew install --cask mactex-no-gui`)",
    )


def _embeddings() -> Finding:
    if importlib.util.find_spec("fastembed"):
        return Finding("embeddings", True, OPTIONAL, "fastembed installed")
    return Finding(
        "embeddings",
        False,
        OPTIONAL,
        "not installed, so alignment ranks candidate pairs by word overlap alone",
        "install with the `embeddings` extra; under the plugin, set SCIBRAID_EXTRAS=embeddings",
    )


def _uv() -> Finding:
    if shutil.which("uv"):
        return Finding("uv", True, OPTIONAL, shutil.which("uv") or "")
    return Finding("uv", False, OPTIONAL, "not on PATH; the plugin's launcher needs it", "https://docs.astral.sh/uv/getting-started/installation/")


def check(offline: bool = False, client: httpx.Client | None = None) -> list[Finding]:
    findings = [
        Finding("python", sys.version_info >= (3, 13), REQUIRED, sys.version.split()[0], "scibraid needs Python 3.13 or later"),
        _data_directory(),
        _openalex_key(),
    ]
    if not offline:
        findings.insert(2, _openalex_reachable(client))
    findings += [_pdf_reader(), _latex(), _embeddings(), _uv()]
    return findings


def report(findings: list[Finding]) -> str:
    missing = {REQUIRED: "FAIL", RECOMMENDED: "warn", OPTIONAL: "note"}
    lines = []
    for f in findings:
        mark = "ok  " if f.ok else missing[f.level]
        lines.append(f"[{mark}] {f.name:<15} {f.detail}")
        if not f.ok and f.fix:
            lines.append(f"       {'':<15} fix: {f.fix}")
    failed = [f for f in findings if not f.ok and f.level == REQUIRED]
    lines.append("")
    lines.append(f"{len(failed)} required check(s) failed." if failed else "Nothing required is missing.")
    return "\n".join(lines)


def ok(findings: list[Finding]) -> bool:
    return not any(not f.ok and f.level == REQUIRED for f in findings)
