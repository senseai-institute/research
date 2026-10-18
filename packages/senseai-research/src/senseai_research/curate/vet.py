# SPDX-License-Identifier: Apache-2.0
"""Vet sources: retractions (hard flag), PubPeer (soft flag), venue checks, citation intents.

- Crossref ``update-to`` / ``relation`` entries of type retraction: the claim
  is discarded and the paper shown in the sheet for the record.
- PubPeer: number of comment threads; flagged, never judged.
- Venue: OpenAlex source not in DOAJ *and* low citation count, or on a local
  predatory-publisher list (``$SENSEAI_DATA/predatory.txt``, one name per line).
- Citation intents (Semantic Scholar, free): how many later papers cite the
  paper as a result they build on versus merely as background.
- Reference lists (Crossref, OpenAlex) feed the citation-chain detection in
  ``reconcile``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from senseai_research.curate.cache import CachedClient, CacheMiss, data_root
from senseai_research.curate.search import Paper, norm_doi

CROSSREF = "https://api.crossref.org/works/{doi}"
PUBPEER = "https://pubpeer.com/api/v2/publications"
S2_CITATIONS = "https://api.semanticscholar.org/graph/v1/paper/DOI:{doi}/citations"
OPENALEX_WORK = "https://api.openalex.org/works/https://doi.org/{doi}"


@dataclass
class Vetting:
    doi: str
    retracted: bool = False
    retraction_note: str = ""
    pubpeer_threads: int = 0
    pubpeer_url: str | None = None
    venue_flags: list[str] = field(default_factory=list)
    supporting_citations: int = 0
    contrasting_citations: int = 0
    background_citations: int = 0
    references: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def flags(self) -> list[str]:
        out: list[str] = []
        if self.retracted:
            out.append("retracted")
        if self.pubpeer_threads:
            out.append(f"pubpeer:{self.pubpeer_threads}")
        out.extend(self.venue_flags)
        if self.contrasting_citations:
            out.append(f"contrasting_citations:{self.contrasting_citations}")
        return out


def crossref(client: CachedClient, v: Vetting) -> None:
    try:
        r = client.get(
            CROSSREF.format(doi=v.doi), params={"mailto": client.email} if client.email else None
        )
    except CacheMiss as e:
        v.errors.append(f"crossref: {e}")
        return
    if not r.ok:
        v.errors.append(f"crossref: HTTP {r.status}")
        return
    msg = (r.json() or {}).get("message", {})
    for u in msg.get("update-to", []) or []:
        if str(u.get("type", "")).lower() == "retraction":
            v.retracted = True
            when = (u.get("updated") or {}).get("date-time", "")
            v.retraction_note = f"retraction notice {u.get('DOI', '')} {when}".strip()
    rel = msg.get("relation", {}) or {}
    if rel.get("is-retracted-by"):
        v.retracted = True
        v.retraction_note = v.retraction_note or "Crossref relation is-retracted-by"
    for ref in msg.get("reference", []) or []:
        d = norm_doi(ref.get("DOI"))
        if d and d not in v.references:
            v.references.append(d)


def openalex_references(client: CachedClient, v: Vetting, paper: Paper | None = None) -> None:
    """Fill references from OpenAlex when Crossref had none (needs the paper's referenced_works)."""
    if v.references or paper is None or not paper.referenced_dois:
        return
    v.references = list(paper.referenced_dois)


def pubpeer(client: CachedClient, v: Vetting) -> None:
    key = os.environ.get("PUBPEER_API_KEY", "")
    if not key:
        return
    try:
        r = client.get(PUBPEER, params={"doi": v.doi, "key": key})
    except CacheMiss as e:
        v.errors.append(f"pubpeer: {e}")
        return
    if not r.ok:
        v.errors.append(f"pubpeer: HTTP {r.status}")
        return
    for pub in (r.json() or {}).get("publications", []):
        v.pubpeer_threads += int(pub.get("comments", 0) or 0)
        v.pubpeer_url = pub.get("url") or v.pubpeer_url


def predatory_list() -> set[str]:
    p = data_root() / "predatory.txt"
    if not p.exists():
        return set()
    return {line.strip().lower() for line in p.read_text().splitlines() if line.strip()}


def venue(client: CachedClient, v: Vetting, paper: Paper, *, min_citations: int = 5) -> None:
    pred = predatory_list()
    if paper.venue and paper.venue.lower() in pred:
        v.venue_flags.append("predatory_venue")
    try:
        r = client.get(
            OPENALEX_WORK.format(doi=v.doi),
            params={"mailto": client.email} if client.email else None,
        )
    except CacheMiss as e:
        v.errors.append(f"openalex: {e}")
        return
    if not r.ok:
        return
    w = r.json() or {}
    src = (w.get("primary_location") or {}).get("source") or {}
    if (
        src
        and not src.get("is_in_doaj", True)
        and int(w.get("cited_by_count") or 0) < min_citations
    ):
        v.venue_flags.append("low_citations_non_doaj")


def citation_intents(client: CachedClient, v: Vetting, *, limit: int = 100) -> None:
    headers = {}
    if key := os.environ.get("S2_API_KEY"):
        headers["x-api-key"] = key
    try:
        r = client.get(
            S2_CITATIONS.format(doi=v.doi),
            params={"fields": "intents,isInfluential", "limit": limit},
            headers=headers,
        )
    except CacheMiss as e:
        v.errors.append(f"s2: {e}")
        return
    if not r.ok:
        return
    for c in (r.json() or {}).get("data", []):
        intents = set(c.get("intents") or [])
        if intents & {"result", "methodology"}:
            v.supporting_citations += 1
        elif "background" in intents:
            v.background_citations += 1
    # S2 does not expose a contradiction intent; scite.ai does (optional, paid). Left at 0.


def vet(client: CachedClient, paper: Paper) -> Vetting:
    v = Vetting(paper.doi)
    crossref(client, v)
    openalex_references(client, v, paper)
    pubpeer(client, v)
    venue(client, v, paper)
    citation_intents(client, v)
    return v


__all__ = ["Vetting", "citation_intents", "crossref", "pubpeer", "venue", "vet"]
