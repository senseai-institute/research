# SPDX-License-Identifier: Apache-2.0
"""Literature search: build queries per catalogue path, run them, deduplicate by DOI.

Sources: OpenAlex (primary), Semantic Scholar Graph, PubMed E-utilities,
Europe PMC REST. bioRxiv/medRxiv have no query search; preprints come in via
Europe PMC (``SRC:PPR``). Every query and its hit count is kept in the query
log so the review sheet can show what was tried.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from senseai_research.curate.cache import CachedClient, CacheMiss
from senseai_research.curate.catalogue import Catalogue, PathSpec

OPENALEX = "https://api.openalex.org/works"
S2 = "https://api.semanticscholar.org/graph/v1/paper/search"
PUBMED = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
EUROPEPMC = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"


@dataclass
class Paper:
    doi: str
    title: str
    year: int | None = None
    abstract: str | None = None
    pmcid: str | None = None
    pmid: str | None = None
    openalex_id: str | None = None
    s2_id: str | None = None
    venue: str | None = None
    is_oa: bool = False
    oa_url: str | None = None
    cited_by: int = 0
    kind: str = "article"  # article | review | preprint | dataset | other
    referenced_dois: list[str] = field(default_factory=list)
    found_by: list[str] = field(default_factory=list)  # "openalex:<query>"
    paths: set[str] = field(default_factory=set)  # catalogue paths whose queries hit it

    def merge(self, other: Paper) -> None:
        for f in ("year", "abstract", "pmcid", "pmid", "openalex_id", "s2_id", "venue", "oa_url"):
            if getattr(self, f) in (None, "") and getattr(other, f):
                setattr(self, f, getattr(other, f))
        self.is_oa = self.is_oa or other.is_oa
        self.cited_by = max(self.cited_by, other.cited_by)
        if self.kind == "article" and other.kind != "article":
            self.kind = other.kind
        for d in other.referenced_dois:
            if d not in self.referenced_dois:
                self.referenced_dois.append(d)
        self.found_by.extend(f for f in other.found_by if f not in self.found_by)
        self.paths |= other.paths


@dataclass(frozen=True, slots=True)
class Query:
    path: str
    text: str
    source: str
    hits: int


def norm_doi(doi: str | None) -> str | None:
    if not doi:
        return None
    d = doi.strip().lower()
    d = re.sub(r"^https?://(dx\.)?doi\.org/", "", d)
    d = d.removeprefix("doi:")
    return d or None


def build_queries(names: Iterable[str], spec: PathSpec, *, max_queries: int = 4) -> list[str]:
    """``<name> <organ term> <measure term>`` combinations, most specific first."""
    out: list[str] = []
    names_l = list(names)[:2]  # canonical + best synonym/common name
    for name in names_l:
        for measure in spec.measure_terms[:2]:
            for organ in spec.organ_terms[:2]:
                out.append(f"{name} {organ} {measure}")
                if len(out) >= max_queries:
                    return out
    return out


# --- source clients -----------------------------------------------------------


def _abstract_from_inverted(idx: dict[str, list[int]] | None) -> str | None:
    if not idx:
        return None
    pos: dict[int, str] = {}
    for word, places in idx.items():
        for p in places:
            pos[p] = word
    return " ".join(pos[i] for i in sorted(pos)) or None


def openalex_search(client: CachedClient, text: str, *, per_page: int = 25) -> list[Paper]:
    params: dict[str, Any] = {
        "search": text,
        "per-page": per_page,
        "select": (
            "id,doi,title,publication_year,type,cited_by_count,abstract_inverted_index,"
            "primary_location,open_access,ids,referenced_works"
        ),
    }
    if client.email:
        params["mailto"] = client.email
    d = client.get_json(OPENALEX, params=params)
    out: list[Paper] = []
    for w in d.get("results", []):
        doi = norm_doi(w.get("doi"))
        if not doi:
            continue
        ids = w.get("ids", {}) or {}
        pmcid = (ids.get("pmcid") or "").rsplit("/", 1)[-1] or None
        pmid = (ids.get("pmid") or "").rsplit("/", 1)[-1] or None
        loc = w.get("primary_location") or {}
        src = loc.get("source") or {}
        oa = w.get("open_access") or {}
        out.append(
            Paper(
                doi=doi,
                title=str(w.get("title") or ""),
                year=w.get("publication_year"),
                abstract=_abstract_from_inverted(w.get("abstract_inverted_index")),
                pmcid=pmcid,
                pmid=pmid,
                openalex_id=str(w.get("id") or "").rsplit("/", 1)[-1] or None,
                venue=src.get("display_name"),
                is_oa=bool(oa.get("is_oa")),
                oa_url=oa.get("oa_url"),
                cited_by=int(w.get("cited_by_count") or 0),
                kind="review"
                if w.get("type") == "review"
                else ("preprint" if w.get("type") == "preprint" else "article"),
            )
        )
    return out


def s2_search(client: CachedClient, text: str, *, limit: int = 20) -> list[Paper]:
    headers = {}
    if key := os.environ.get("S2_API_KEY"):
        headers["x-api-key"] = key
    d = client.get_json(
        S2,
        params={
            "query": text,
            "limit": limit,
            "fields": (
                "externalIds,title,abstract,year,citationCount,venue,isOpenAccess,"
                "openAccessPdf,publicationTypes"
            ),
        },
        headers=headers,
    )
    out: list[Paper] = []
    for p in d.get("data", []):
        doi = norm_doi((p.get("externalIds") or {}).get("DOI"))
        if not doi:
            continue
        types = p.get("publicationTypes") or []
        out.append(
            Paper(
                doi=doi,
                title=str(p.get("title") or ""),
                year=p.get("year"),
                abstract=p.get("abstract"),
                s2_id=p.get("paperId"),
                venue=p.get("venue") or None,
                is_oa=bool(p.get("isOpenAccess")),
                oa_url=(p.get("openAccessPdf") or {}).get("url"),
                cited_by=int(p.get("citationCount") or 0),
                kind="review" if "Review" in types else "article",
            )
        )
    return out


def pubmed_search(client: CachedClient, text: str, *, retmax: int = 20) -> list[Paper]:
    params: dict[str, Any] = {"db": "pubmed", "term": text, "retmax": retmax, "retmode": "json"}
    if key := os.environ.get("NCBI_API_KEY"):
        params["api_key"] = key
    ids = (
        client.get_json(f"{PUBMED}/esearch.fcgi", params=params)
        .get("esearchresult", {})
        .get("idlist", [])
    )
    if not ids:
        return []
    sparams: dict[str, Any] = {"db": "pubmed", "id": ",".join(ids), "retmode": "json"}
    if key:
        sparams["api_key"] = key
    res = client.get_json(f"{PUBMED}/esummary.fcgi", params=sparams).get("result", {})
    out: list[Paper] = []
    for uid in res.get("uids", []):
        r = res.get(uid, {})
        aids = {a.get("idtype"): a.get("value") for a in r.get("articleids", [])}
        doi = norm_doi(aids.get("doi"))
        if not doi:
            continue
        year = None
        if m := re.match(r"(\d{4})", str(r.get("pubdate", ""))):
            year = int(m.group(1))
        out.append(
            Paper(
                doi=doi,
                title=str(r.get("title") or ""),
                year=year,
                pmid=str(uid),
                pmcid=aids.get("pmc"),
                venue=r.get("fulljournalname"),
            )
        )
    return out


def europepmc_search(client: CachedClient, text: str, *, page_size: int = 25) -> list[Paper]:
    d = client.get_json(
        EUROPEPMC,
        params={"query": text, "format": "json", "pageSize": page_size, "resultType": "lite"},
    )
    out: list[Paper] = []
    for r in (d.get("resultList") or {}).get("result", []):
        doi = norm_doi(r.get("doi"))
        if not doi:
            continue
        kind = (
            "preprint"
            if r.get("source") == "PPR"
            else ("review" if "Review" in str(r.get("pubType", "")) else "article")
        )
        out.append(
            Paper(
                doi=doi,
                title=str(r.get("title") or ""),
                year=int(r["pubYear"]) if str(r.get("pubYear", "")).isdigit() else None,
                abstract=r.get("abstractText"),
                pmcid=r.get("pmcid"),
                pmid=r.get("pmid"),
                venue=r.get("journalTitle"),
                is_oa=r.get("isOpenAccess") == "Y",
                cited_by=int(r.get("citedByCount") or 0),
                kind=kind,
            )
        )
    return out


SOURCES: dict[str, Callable[[CachedClient, str], list[Paper]]] = {
    "openalex": openalex_search,
    "europepmc": europepmc_search,
    "pubmed": pubmed_search,
    "s2": s2_search,
}


# --- driver --------------------------------------------------------------------


@dataclass
class SearchResult:
    papers: dict[str, Paper]
    queries: list[Query]
    errors: list[str]

    def by_path(self, path: str) -> list[Paper]:
        return [p for p in self.papers.values() if path in p.paths]


def search(
    client: CachedClient,
    names: Iterable[str],
    catalogue: Catalogue,
    *,
    sources: Iterable[str] = ("openalex", "europepmc", "pubmed", "s2"),
    max_queries_per_path: int = 3,
    max_papers: int = 60,
) -> SearchResult:
    """Run every query for every catalogue path; return unique papers keyed by DOI."""
    papers: dict[str, Paper] = {}
    log: list[Query] = []
    errors: list[str] = []
    names_l = list(names)
    for spec in catalogue.paths:
        for text in build_queries(names_l, spec, max_queries=max_queries_per_path):
            for src in sources:
                fn = SOURCES[src]
                try:
                    hits = fn(client, text)
                except CacheMiss as e:
                    errors.append(f"{src}: {e}")
                    log.append(Query(spec.path, text, src, -1))
                    continue
                except Exception as e:  # one dead source must not stop the run
                    errors.append(f"{src} {text!r}: {type(e).__name__}: {e}")
                    log.append(Query(spec.path, text, src, -1))
                    continue
                log.append(Query(spec.path, text, src, len(hits)))
                for h in hits:
                    h.found_by.append(f"{src}:{text}")
                    h.paths.add(spec.path)
                    if h.doi in papers:
                        papers[h.doi].merge(h)
                    else:
                        papers[h.doi] = h
    ranked = sorted(papers.values(), key=lambda p: (-len(p.paths), -p.cited_by, p.doi))[:max_papers]
    return SearchResult({p.doi: p for p in ranked}, log, errors)


__all__ = [
    "EUROPEPMC",
    "OPENALEX",
    "PUBMED",
    "S2",
    "SOURCES",
    "Paper",
    "Query",
    "SearchResult",
    "build_queries",
    "norm_doi",
    "search",
]
