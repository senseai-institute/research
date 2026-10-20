# SPDX-License-Identifier: Apache-2.0
"""Thin facade over ``senseai_research.curate``: search, fetch, extract, vet, units, cache.

The curate pipeline lives in this package since phase 5 (spec D2). Everything goes through
curate's :class:`CachedClient`, so tests run offline against a fixture cache and a live
box reuses the same on-disk cache (and its S3 mirror).
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from senseai_research.curate.cache import CachedClient, ModelCache
    from senseai_research.curate.extract import Extraction, ModelCall
    from senseai_research.curate.fetch import PaperText
    from senseai_research.curate.search import Paper
    from senseai_research.curate.vet import Vetting

SEARCH_SOURCES = ("openalex", "europepmc", "pubmed", "s2")


def client(cache_root: Path, *, offline: bool | None = None) -> CachedClient:
    """curate's cached HTTP client rooted at ``cache_root`` (``<root>/http``)."""
    from senseai_research.curate.cache import CachedClient

    return CachedClient(cache_root, offline=offline)


def model_cache(cache_root: Path) -> ModelCache:
    from senseai_research.curate.cache import ModelCache

    return ModelCache(cache_root)


def normalise_unit(unit: str) -> tuple[str, float] | None:
    """``(canonical_unit, factor)`` per curate's units table, or ``None``."""
    try:
        from senseai_research.curate.units import normalise_unit as nu
    except ImportError:  # pint missing: literal units still work through the facade
        return None
    got: tuple[str, float] | None = nu(unit)
    return got


def value_in_quote(value: float, quote: str) -> bool:
    """curate's check that a number appears in a quote (``extract._value_in_quote``)."""
    from senseai_research.curate.extract import _value_in_quote

    return bool(_value_in_quote(value, quote))


def search(
    c: CachedClient,
    query: str,
    *,
    sources: Iterable[str] = SEARCH_SOURCES,
    since: int | None = None,
    limit: int = 10,
) -> tuple[list[Paper], list[str]]:
    """Run one free-text query on each source; merge by DOI. Returns ``(papers, errors)``."""
    from senseai_research.curate.cache import CacheMiss
    from senseai_research.curate.search import SOURCES

    papers: dict[str, Paper] = {}
    errors: list[str] = []
    for src in sources:
        try:
            hits = SOURCES[src](c, query)
        except CacheMiss:
            errors.append(f"{src}: not in the offline cache")
            continue
        except Exception as e:  # one dead source must not stop the search
            errors.append(f"{src}: {type(e).__name__}")
            continue
        for h in hits:
            h.found_by.append(f"{src}:{query}")
            if h.doi in papers:
                papers[h.doi].merge(h)
            else:
                papers[h.doi] = h
    found = [p for p in papers.values() if since is None or (p.year or 0) >= since]
    found.sort(key=lambda p: (-len(p.found_by), -p.cited_by, p.doi))
    return found[:limit], errors


def lookup(c: CachedClient, doi: str) -> Paper:
    """Bibliographic record for one DOI (Europe PMC, which also gives the PMCID)."""
    from senseai_research.curate.cache import CacheMiss
    from senseai_research.curate.search import Paper, europepmc_search, norm_doi

    d = norm_doi(doi) or doi
    try:
        hits = europepmc_search(c, f'DOI:"{d}"', page_size=1)
    except (CacheMiss, Exception):
        hits = []
    for h in hits:
        if h.doi == d:
            return h
    return Paper(doi=d, title="")


def fetch(c: CachedClient, paper: Paper) -> PaperText:
    """Best open-access text (curate ``fetch_text``); never paywalled content."""
    from senseai_research.curate.fetch import fetch_text

    return fetch_text(c, paper)


def vet(c: CachedClient, paper: Paper) -> Vetting:
    from senseai_research.curate.vet import vet as _vet

    return _vet(c, paper)


def extract_path(
    text: PaperText,
    library_path: str,
    *,
    cache: ModelCache,
    call: ModelCall | None = None,
) -> Extraction:
    """curate ``extract`` with the catalogue restricted to one library path.

    ``library_path`` is the full path (``felidae.senses.vision.detection_threshold``); the
    catalogue speaks in clade-relative paths.
    """
    from senseai_research.curate.catalogue import Catalogue, catalogue_for
    from senseai_research.curate.extract import extract

    clade, rel = library_path.split(".", 1)
    parts = rel.split(".")
    senses = [parts[1]] if parts[0] == "senses" else []
    full = catalogue_for(clade, senses)
    keep = tuple(p for p in full.paths if p.path == rel)
    if not keep:
        raise ValueError(f"{library_path} is not a path curate knows how to extract")
    cat = Catalogue(full.clade_key, keep, full.consumed)
    return extract(text, cat, cache=cache, call=call)


def run_state_dir() -> Path:
    """Where curate keeps its run states (``$SENSEAI_DATA/curate-cache/runs``)."""
    root = Path(os.environ.get("SENSEAI_DATA", Path.home() / ".senseai")).expanduser()
    return root / "curate-cache" / "runs"


def paper_dict(p: Paper) -> dict[str, Any]:
    from dataclasses import asdict

    d = asdict(replace(p, paths=set()))
    d.pop("paths", None)
    return d


__all__ = [
    "SEARCH_SOURCES",
    "client",
    "extract_path",
    "fetch",
    "lookup",
    "model_cache",
    "normalise_unit",
    "paper_dict",
    "run_state_dir",
    "search",
    "value_in_quote",
    "vet",
]
