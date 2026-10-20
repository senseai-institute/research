# SPDX-License-Identifier: Apache-2.0
"""The draft pipeline with its persisted run state.

Each step reads and writes ``$SENSEAI_DATA/curate-cache/runs/<clade>.json`` so
the steps can be run one at a time (``senseai-library search felidae`` …) or in
one go (``senseai-library draft felidae``). Everything the review sheet shows
comes from this state plus the YAML; nothing is kept only in memory.
"""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from senseai_research.curate import assemble as asm
from senseai_research.curate import extract as ex
from senseai_research.curate import fetch as fe
from senseai_research.curate import reconcile as rc
from senseai_research.curate import review as rv
from senseai_research.curate import search as se
from senseai_research.curate import taxonomy as tx
from senseai_research.curate import vet as vt
from senseai_research.curate.cache import CachedClient, ModelCache, data_root
from senseai_research.curate.catalogue import Catalogue, catalogue_for


@dataclass
class RunState:
    clade_key: str
    senses: list[str]
    taxon: dict[str, Any] | None = None
    papers: dict[str, dict[str, Any]] = field(default_factory=dict)
    queries: list[dict[str, Any]] = field(default_factory=list)
    search_errors: list[str] = field(default_factory=list)
    texts: dict[str, dict[str, Any]] = field(default_factory=dict)
    extractions: dict[str, dict[str, Any]] = field(default_factory=dict)
    vettings: dict[str, dict[str, Any]] = field(default_factory=dict)
    resolved: dict[str, dict[str, Any]] = field(default_factory=dict)
    unresolved: list[str] = field(default_factory=list)
    merge_log: dict[str, Any] = field(default_factory=dict)
    cache_digest: str = ""
    curated_at: str = ""

    # -- persistence --------------------------------------------------------------
    @staticmethod
    def path(clade_key: str) -> Path:
        return data_root() / "curate-cache" / "runs" / f"{clade_key}.json"

    def save(self) -> Path:
        p = self.path(self.clade_key)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(asdict(self), indent=1, default=_json_default))
        return p

    @classmethod
    def load(cls, clade_key: str, senses: Iterable[str] = ()) -> RunState:
        p = cls.path(clade_key)
        if not p.exists():
            return cls(clade_key, list(senses))
        d = json.loads(p.read_text())
        if senses:
            d["senses"] = list(senses)
        return cls(**d)

    # -- views ------------------------------------------------------------------------
    def paper_objs(self) -> dict[str, se.Paper]:
        return {k: _paper(v) for k, v in self.papers.items()}

    def claims(self) -> list[ex.Claim]:
        out: list[ex.Claim] = []
        for e in self.extractions.values():
            out += [ex.Claim(**c) for c in e.get("claims", [])]
        return out

    def catalogue(self) -> Catalogue:
        return catalogue_for(self.clade_key, self.senses)

    def names(self) -> list[str]:
        if self.taxon:
            return list(tx.Taxon(**_taxon_kwargs(self.taxon)).search_names)
        return [self.clade_key.capitalize()]


def _json_default(o: Any) -> Any:
    if isinstance(o, set):
        return sorted(o)
    if dataclasses.is_dataclass(o) and not isinstance(o, type):
        return asdict(o)
    raise TypeError(type(o).__name__)


def _paper(d: dict[str, Any]) -> se.Paper:
    d = dict(d)
    d["paths"] = set(d.get("paths", []))
    return se.Paper(**d)


def _taxon_kwargs(d: dict[str, Any]) -> dict[str, Any]:
    d = dict(d)
    d["children"] = [tx.Species(**c) if isinstance(c, dict) else c for c in d.get("children", [])]
    return d


# --- steps --------------------------------------------------------------------------


def step_taxonomy(client: CachedClient, st: RunState, name: str | None = None) -> tx.Taxon:
    t = tx.resolve(client, name or st.clade_key.capitalize())
    st.taxon = t.to_dict()
    return t


def step_search(client: CachedClient, st: RunState, **kw: Any) -> se.SearchResult:
    res = se.search(client, st.names(), st.catalogue(), **kw)
    st.papers = {k: asdict(p) for k, p in res.papers.items()}
    st.queries = [asdict(q) for q in res.queries]
    st.search_errors = res.errors
    return res


def step_fetch(client: CachedClient, st: RunState) -> dict[str, fe.PaperText]:
    out: dict[str, fe.PaperText] = {}
    for doi, p in st.paper_objs().items():
        t = fe.fetch_text(client, p)
        out[doi] = t
        st.texts[doi] = asdict(t)
    return out


def step_extract(
    st: RunState, *, cache: ModelCache | None = None, call: ex.ModelCall | None = None
) -> dict[str, ex.Extraction]:
    cache = cache or ModelCache()
    cat = st.catalogue()
    out: dict[str, ex.Extraction] = {}
    for doi, td in st.texts.items():
        text = fe.PaperText(**{**td, "sections": tuple(tuple(s) for s in td.get("sections", []))})
        e = ex.extract(text, cat, cache=cache, call=call)
        out[doi] = e
        st.extractions[doi] = {
            "status": e.status,
            "paper_type": e.paper_type,
            "species_studied": e.species_studied,
            "claims": [c.to_dict() for c in e.claims],
            "rejected": [{"claim": r, "reason": why} for r, why in e.rejected],
        }
    return out


def step_vet(client: CachedClient, st: RunState) -> dict[str, vt.Vetting]:
    out: dict[str, vt.Vetting] = {}
    for doi, p in st.paper_objs().items():
        if doi not in st.extractions or not st.extractions[doi].get("claims"):
            continue
        v = vt.vet(client, p)
        out[doi] = v
        st.vettings[doi] = asdict(v) | {"flags": v.flags}
    return out


def step_reconcile(st: RunState) -> rc.Reconciliation:
    retracted = {d for d, v in st.vettings.items() if v.get("retracted")}
    chains = {d: v.get("references", []) for d, v in st.vettings.items()}
    claims = [c for c in st.claims() if c.doi not in retracted]
    # species outside the clade: keep, flag
    known = _known_species(st)
    for c in claims:
        if (
            known
            and c.species
            and c.species.lower() not in known
            and "species_outside_clade" not in c.flags
        ):
            c.flags.append("species_outside_clade")
    rec = rc.reconcile(claims, [p.path for p in st.catalogue().paths], chains=chains)
    st.resolved = {
        p: {
            "value": list(r.value) if isinstance(r.value, tuple) else r.value,
            "unit": r.unit,
            "confidence": r.confidence,
            "primary_doi": r.primary_doi,
            "supporting": [c.to_dict() for c in r.supporting],
            "conflicting": [c.to_dict() for c in r.conflicting],
            "note": r.note,
            "independent": r.independent,
        }
        for p, r in rec.resolved.items()
    }
    st.unresolved = list(rec.unresolved_paths)
    return rec


def _known_species(st: RunState) -> set[str]:
    if not st.taxon:
        return set()
    t = st.taxon
    out = {str(t.get("canonical", "")).lower()}
    out |= {str(c.get("name", "")).lower() for c in t.get("children", []) if isinstance(c, dict)}
    out |= {s.lower() for s in t.get("synonyms", [])}
    return out - {""}


def _reconciliation(st: RunState) -> rc.Reconciliation:
    rec = rc.Reconciliation(unresolved_paths=list(st.unresolved))
    for p, d in st.resolved.items():
        v = tuple(d["value"]) if isinstance(d["value"], list) else d["value"]
        rec.resolved[p] = rc.Resolved(
            p,
            v,
            d["unit"],
            d["confidence"],
            d["primary_doi"],
            [ex.Claim(**c) for c in d["supporting"]],
            [ex.Claim(**c) for c in d["conflicting"]],
            d.get("note", ""),
            int(d.get("independent", 0)),
        )
    return rec


def step_assemble(
    st: RunState, library_dir: Path, *, cache_digest: str = ""
) -> tuple[dict[str, Any], Path]:
    entry_path = library_dir / f"{st.clade_key}.yaml"
    existing = asm.load_entry_data(entry_path)
    t = st.taxon or {}
    species = [
        {"name": c["name"], "common_name": (c.get("common_names") or [c["name"]])[0]}
        for c in t.get("children", [])[:3]
        if isinstance(c, dict)
    ]
    data, log = asm.assemble(
        existing,
        _reconciliation(st),
        st.catalogue(),
        clade=t.get("canonical") or st.clade_key.capitalize(),
        rank=t.get("rank") or "family",
        common_names=t.get("common_names", []),
        species=species,
        queries=[q["text"] for q in st.queries],
        cache_digest=cache_digest or st.cache_digest,
        papers_screened=len(st.papers),
        claims_extracted=sum(len(e.get("claims", [])) for e in st.extractions.values()),
    )
    st.cache_digest = cache_digest or st.cache_digest
    st.curated_at = data["curated_at"]
    st.merge_log = asdict(log)
    asm.dump_entry(data, entry_path)
    return data, entry_path


def sheet_input(st: RunState, entry: dict[str, Any]) -> rv.SheetInput:
    per_source: dict[str, int] = {}
    for paper in st.papers.values():
        for fb in paper.get("found_by", []):
            src = fb.split(":", 1)[0]
            per_source[src] = per_source.get(src, 0) + 1
    fulltext = sum(1 for t in st.texts.values() if t.get("fulltext"))
    abstract_only = sum(1 for t in st.texts.values() if not t.get("fulltext") and t.get("text"))
    counts = {"high": 0, "medium": 0, "low": 0}
    for r in st.resolved.values():
        counts[r["confidence"]] = counts.get(r["confidence"], 0) + 1
    flags_detail: dict[str, list[str]] = {"retraction": [], "pubpeer": [], "venue": []}
    paper_flags: dict[str, list[str]] = {}
    for doi, v in st.vettings.items():
        if v.get("retracted"):
            flags_detail["retraction"].append(
                f"{doi}: {v.get('retraction_note', '')}; its claims were discarded, "
                "shown for the record"
            )
        if v.get("pubpeer_threads"):
            flags_detail["pubpeer"].append(
                f"{doi} has {v['pubpeer_threads']} thread(s) "
                f"({v.get('pubpeer_url') or 'pubpeer.com'}); content not judged, read it"
            )
        for f in v.get("venue_flags", []):
            flags_detail["venue"].append(f"{doi}: {f}")
        if v.get("flags"):
            paper_flags[doi] = list(v["flags"])
    agreement: dict[str, str] = {}
    for path, r in st.resolved.items():
        if r["conflicting"]:
            vals = " vs ".join(
                f"{_num(c)} ({c['doi']})" for c in [r["supporting"][0], *r["conflicting"]]
            )
            agreement[path] = f"**conflict**: {vals}"
        elif len({c["doi"] for c in r["supporting"]}) > 1:
            agreement[path] = "agree (±10 %)"
        else:
            agreement[path] = "single"
        claim_flags = sorted({f for c in r["supporting"] for f in c.get("flags", [])})
        if claim_flags:
            paper_flags.setdefault(r["primary_doi"], [])
            paper_flags[r["primary_doi"]] = sorted(
                set(paper_flags[r["primary_doi"]]) | set(claim_flags)
            )
    empty: dict[str, str] = {}
    by_path: dict[str, list[dict[str, Any]]] = {}
    for q in st.queries:
        by_path.setdefault(q["path"], []).append(q)
    for path in st.unresolved:
        qs = by_path.get(path, [])
        hits = sum(int(q.get("hits", 0)) for q in qs)
        empty[path] = f"{len(qs)} queries, {hits} hits, 0 claims"
    return rv.SheetInput(
        clade_key=st.clade_key,
        curated_at=st.curated_at,
        cache_digest=st.cache_digest,
        papers_screened=len(st.papers),
        per_source=per_source,
        fulltext=fulltext,
        abstract_only=abstract_only,
        claims_extracted=sum(len(e.get("claims", [])) for e in st.extractions.values()),
        counts=counts,
        flags_detail=flags_detail,
        paper_flags=paper_flags,
        agreement=agreement,
        empty_paths=empty,
        queries=[f"{q['text']} [{q['source']}: {q['hits']}]" for q in st.queries],
        placeholders=st.merge_log.get("placeholders", []),
    )


def _num(c: dict[str, Any]) -> str:
    v = c.get("canonical_value")
    if v is None and c.get("canonical_low") is not None:
        return f"{c['canonical_low']:g}–{c['canonical_high']:g}"
    return f"{v:g}" if v is not None else "?"


def step_review(st: RunState, library_dir: Path) -> Path:
    entry = asm.load_entry_data(library_dir / f"{st.clade_key}.yaml")
    if entry is None:
        raise FileNotFoundError(f"no draft entry for {st.clade_key}; run draft first")
    out = library_dir / "review" / f"{st.clade_key}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(rv.render_sheet(entry, sheet_input(st, entry)))
    return out


def summary(st: RunState) -> dict[str, Any]:
    counts = {"high": 0, "medium": 0, "low": 0}
    for r in st.resolved.values():
        counts[r["confidence"]] += 1
    return {
        "clade": st.clade_key,
        "papers": len(st.papers),
        "texts": sum(1 for t in st.texts.values() if t.get("text")),
        "fulltext": sum(1 for t in st.texts.values() if t.get("fulltext")),
        "claims": sum(len(e.get("claims", [])) for e in st.extractions.values()),
        "rejected": sum(len(e.get("rejected", [])) for e in st.extractions.values()),
        "values": counts,
        "flags": sorted({f for v in st.vettings.values() for f in v.get("flags", [])}),
        "empty_paths": list(st.unresolved),
        "search_errors": st.search_errors,
    }


__all__ = [
    "RunState",
    "sheet_input",
    "step_assemble",
    "step_extract",
    "step_fetch",
    "step_reconcile",
    "step_review",
    "step_search",
    "step_taxonomy",
    "step_vet",
    "summary",
]
