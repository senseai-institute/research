# SPDX-License-Identifier: Apache-2.0
"""Resolve a clade: canonical name, rank, synonyms, children, common names, conservation, links.

Sources: GBIF Backbone (canonical name, rank, synonyms, children), Open Tree of
Life (cross-check of the name), Catalogue of Life (synonyms, common names),
IUCN Red List v4 (status per species; needs ``IUCN_API_TOKEN``), Wikidata
SPARQL (Q-id and cross-links). Every call goes through the cache. A source
that is unreachable or returns nothing is recorded in ``Taxon.notes`` and the
rest proceeds; nothing here invents a name.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any

from senseai_research.curate.cache import CachedClient, CacheMiss

GBIF = "https://api.gbif.org/v1"
OTOL = "https://api.opentreeoflife.org/v3"
COL = "https://api.checklistbank.org/dataset/3LR"
IUCN = "https://api.iucnredlist.org/api/v4"
WIKIDATA = "https://query.wikidata.org/sparql"


@dataclass(frozen=True, slots=True)
class Species:
    name: str
    gbif_key: int | None = None
    common_names: tuple[str, ...] = ()
    iucn: str | None = None  # IUCN code
    iucn_url: str | None = None


@dataclass
class Taxon:
    query: str
    canonical: str
    rank: str
    gbif_key: int | None = None
    synonyms: list[str] = field(default_factory=list)
    common_names: list[str] = field(default_factory=list)
    children: list[Species] = field(default_factory=list)
    ott_id: int | None = None
    col_id: str | None = None
    wikidata: str | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def search_names(self) -> list[str]:
        """Names worth searching for: canonical, synonyms, a few common names."""
        out = [self.canonical, *self.synonyms, *self.common_names[:4]]
        seen: list[str] = []
        for n in out:
            if n and n.lower() not in {s.lower() for s in seen}:
                seen.append(n)
        return seen

    def to_dict(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "canonical": self.canonical,
            "rank": self.rank,
            "gbif_key": self.gbif_key,
            "synonyms": self.synonyms,
            "common_names": self.common_names,
            "children": [
                {
                    "name": c.name,
                    "gbif_key": c.gbif_key,
                    "common_names": list(c.common_names),
                    "iucn": c.iucn,
                    "iucn_url": c.iucn_url,
                }
                for c in self.children
            ],
            "ott_id": self.ott_id,
            "col_id": self.col_id,
            "wikidata": self.wikidata,
            "notes": self.notes,
        }


def _safe(client: CachedClient, taxon: Taxon, label: str, url: str, **kw: Any) -> Any:
    try:
        r = client.get(url, **kw)
    except CacheMiss as e:
        taxon.notes.append(f"{label}: {e}")
        return None
    if not r.ok:
        taxon.notes.append(f"{label}: HTTP {r.status}")
        return None
    try:
        return r.json()
    except json.JSONDecodeError:
        taxon.notes.append(f"{label}: non-JSON response")
        return None


def gbif_resolve(client: CachedClient, name: str, *, max_children: int = 50) -> Taxon:
    """GBIF Backbone: match the name, then synonyms, vernacular names and children."""
    m = _safe(
        client,
        Taxon(name, name, "unknown"),
        "gbif.match",
        f"{GBIF}/species/match",
        params={"name": name, "verbose": "false"},
    )
    taxon = Taxon(query=name, canonical=name, rank="unknown")
    if not m or m.get("matchType") in (None, "NONE") or "usageKey" not in m:
        taxon.notes.append("gbif.match: no match")
        return taxon
    key = int(m["usageKey"])
    taxon.gbif_key = key
    taxon.canonical = str(m.get("canonicalName") or m.get("scientificName") or name)
    taxon.rank = str(m.get("rank", "unknown")).lower()
    syn = _safe(
        client, taxon, "gbif.synonyms", f"{GBIF}/species/{key}/synonyms", params={"limit": 50}
    )
    if syn:
        taxon.synonyms = sorted(
            {
                str(s.get("canonicalName") or s.get("scientificName"))
                for s in syn.get("results", [])
                if s.get("canonicalName") or s.get("scientificName")
            }
        )
    vern = _safe(
        client,
        taxon,
        "gbif.vernacular",
        f"{GBIF}/species/{key}/vernacularNames",
        params={"limit": 100},
    )
    if vern:
        names = [
            str(v["vernacularName"])
            for v in vern.get("results", [])
            if v.get("language") in ("eng", None) and v.get("vernacularName")
        ]
        taxon.common_names = sorted(set(names), key=names.index)[:10]
    if taxon.rank in ("family", "order", "subfamily", "genus", "class", "suborder"):
        taxon.children = _gbif_species(client, taxon, key, max_children, depth=2)
    return taxon


def _gbif_species(
    client: CachedClient, taxon: Taxon, key: int, limit: int, *, depth: int
) -> list[Species]:
    """Species under ``key``; genera are expanded one level so a family yields species."""
    ch = _safe(
        client, taxon, "gbif.children", f"{GBIF}/species/{key}/children", params={"limit": limit}
    )
    out: list[Species] = []
    for c in (ch or {}).get("results", []):
        cname = c.get("canonicalName") or c.get("scientificName")
        rank = str(c.get("rank", "")).lower()
        if not cname:
            continue
        if rank == "species":
            out.append(Species(str(cname), int(c["key"]) if "key" in c else None))
        elif rank == "genus" and depth > 0 and "key" in c:
            out.extend(_gbif_species(client, taxon, int(c["key"]), limit, depth=depth - 1))
        if len(out) >= limit:
            break
    return out[:limit]


def otol_check(client: CachedClient, taxon: Taxon) -> None:
    """Open Tree of Life TNRS: confirm the canonical name and record the OTT id."""
    body = json.dumps({"names": [taxon.canonical], "do_approximate_matching": False})
    try:
        r = client.post(
            f"{OTOL}/tnrs/match_names", body=body, headers={"Content-Type": "application/json"}
        )
    except CacheMiss as e:
        taxon.notes.append(f"otol: {e}")
        return
    if not r.ok:
        taxon.notes.append(f"otol: HTTP {r.status}")
        return
    for res in r.json().get("results", []):
        for match in res.get("matches", []):
            t = match.get("taxon", {})
            if t.get("ott_id"):
                taxon.ott_id = int(t["ott_id"])
                for s in t.get("synonyms", []):
                    if s not in taxon.synonyms and s != taxon.canonical:
                        taxon.synonyms.append(str(s))
                return
    taxon.notes.append("otol: no match")


def col_lookup(client: CachedClient, taxon: Taxon) -> None:
    """Catalogue of Life (ChecklistBank, dataset 3LR): id and extra vernacular names."""
    d = _safe(
        client,
        taxon,
        "col.search",
        f"{COL}/nameusage/search",
        params={"q": taxon.canonical, "limit": 5, "content": "SCIENTIFIC_NAME"},
    )
    if not d:
        return
    for res in d.get("result", []):
        usage = res.get("usage", {})
        nm = usage.get("name", {})
        if str(nm.get("scientificName", "")).lower() == taxon.canonical.lower():
            taxon.col_id = str(usage.get("id") or res.get("id") or "")
            break
    if not taxon.col_id:
        taxon.notes.append("col: no exact match")
        return
    v = _safe(client, taxon, "col.vernacular", f"{COL}/nameusage/{taxon.col_id}/vernacular")
    for item in v or []:
        if (
            item.get("language") in ("eng", "en")
            and item.get("name")
            and item["name"] not in taxon.common_names
        ):
            taxon.common_names.append(str(item["name"]))


def iucn_status(client: CachedClient, taxon: Taxon, *, limit: int = 20) -> None:
    """IUCN Red List v4: category per child species (needs IUCN_API_TOKEN)."""
    token = os.environ.get("IUCN_API_TOKEN", "")
    if not token:
        taxon.notes.append("iucn: IUCN_API_TOKEN not set; conservation left for the reviewer")
        return
    out: list[Species] = []
    for sp in taxon.children[:limit]:
        parts = sp.name.split()
        if len(parts) < 2:
            out.append(sp)
            continue
        d = _safe(
            client,
            taxon,
            f"iucn:{sp.name}",
            f"{IUCN}/taxa/scientific_name",
            params={"genus_name": parts[0], "species_name": parts[1]},
            headers={"Authorization": token},
        )
        code, url = None, None
        for a in (d or {}).get("assessments", []):
            if a.get("latest"):
                code = str(a.get("red_list_category_code") or "") or None
                url = a.get("url") or (
                    f"https://www.iucnredlist.org/species/{a.get('sis_taxon_id')}/{a.get('assessment_id')}"
                    if a.get("assessment_id")
                    else None
                )
                break
        out.append(Species(sp.name, sp.gbif_key, sp.common_names, code, url))
    taxon.children = out + taxon.children[limit:]


def wikidata_link(client: CachedClient, taxon: Taxon) -> None:
    """Wikidata: the item for the taxon name (P225) and its GBIF id (P846) as a cross-check."""
    name = taxon.canonical.replace('"', "")
    q = (
        f'SELECT ?item ?gbif WHERE {{ ?item wdt:P225 "{name}" . '
        "OPTIONAL { ?item wdt:P846 ?gbif } } LIMIT 1"
    )
    d = _safe(
        client,
        taxon,
        "wikidata",
        WIKIDATA,
        params={"query": q, "format": "json"},
        headers={"Accept": "application/sparql-results+json"},
    )
    for b in (d or {}).get("results", {}).get("bindings", []):
        taxon.wikidata = str(b["item"]["value"]).rsplit("/", 1)[-1]
        g = b.get("gbif", {}).get("value")
        if g and taxon.gbif_key is not None and str(taxon.gbif_key) != str(g):
            taxon.notes.append(f"wikidata GBIF id {g} differs from GBIF match {taxon.gbif_key}")
        return
    taxon.notes.append("wikidata: no item")


def resolve(client: CachedClient, name: str) -> Taxon:
    """Run every source; each one is optional."""
    t = gbif_resolve(client, name)
    otol_check(client, t)
    col_lookup(client, t)
    iucn_status(client, t)
    wikidata_link(client, t)
    return t


__all__ = [
    "Species",
    "Taxon",
    "col_lookup",
    "gbif_resolve",
    "iucn_status",
    "otol_check",
    "resolve",
    "wikidata_link",
]
