# SPDX-License-Identifier: Apache-2.0
"""Import what exists today into the ledger (spec §4, "Import").

* Every quantity in ``library/*.yaml`` becomes a :class:`Claim` (id ``lib:<path>``), with its
  citations as :class:`Source` records (full references from ``library/CITATIONS.md``).
* A claim is ``extracted`` only if a curate run state holds a checked quote for the same path
  and value **and** the quote is found again in the fetched text stored with that run. Then the
  text is stored and a :class:`Passage` points at the quote. Otherwise it is ``unverified``,
  with the reason (for example, the source was only read as an abstract).
* Every open research request (``research/requests/*.yaml``) and every ``open_questions``
  entry in the library becomes an open :class:`Hypothesis`. An open question that a synced
  request already carries is one hypothesis, not two.

Re-importing is idempotent. A library value that changed upstream becomes a new revision of
its claim, never an overwrite.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import yaml

from senseai_research import literature
from senseai_research.ledger.models import (
    Claim,
    ClaimStatus,
    Hypothesis,
    MappingKind,
    Passage,
    Provenance,
    Quantity,
    Source,
    now,
)
from senseai_research.ledger.store import Store
from senseai_research.literature.passages import locate, normalise

ACTOR = "pipeline:import@0.1.0"
OPEN_REQUEST_STATUSES = ("queued", "in_progress", "answered")


# --- CITATIONS.md -------------------------------------------------------------------------

_LINK = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
_DOI = re.compile(r"^10\.\d{4,9}/\S+$")
_REF = re.compile(
    r"^(?P<authors>.+?) \((?P<year>\d{4})\)\. (?P<title>.+?)(?:\. \*(?P<venue>[^*]+)\*|$)"
)


@dataclass
class CitationRow:
    doi: str | None
    urls: list[str]
    reference: str
    used_for: str
    checked: str
    authors: str = ""
    year: int | None = None
    title: str = ""
    venue: str = ""


def _norm_url(u: str) -> str:
    return u.strip().rstrip("/")


def parse_citations(path: Path) -> list[CitationRow]:
    """Rows of every table in CITATIONS.md: identifier, reference, used for, checked."""
    rows: list[CitationRow] = []
    if not path.exists():
        return rows
    for line in path.read_text().splitlines():
        if not line.startswith("|") or line.startswith("| ---") or line.startswith("| Identifier"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 4:
            continue
        ident, ref, used, checked = cells[:4]
        doi: str | None = None
        urls: list[str] = []
        for tok in (t.strip() for t in ident.split("·")):
            m = _LINK.search(tok)
            if m:
                urls.append(_norm_url(m.group(2)))
            elif _DOI.match(tok):
                doi = tok.lower()
        row = CitationRow(doi, urls, ref, used, checked)
        m2 = _REF.match(ref)
        if m2:
            row.authors = m2.group("authors")
            row.year = int(m2.group("year"))
            row.title = m2.group("title").strip()
            row.venue = (m2.group("venue") or "").strip()
        rows.append(row)
    return rows


def source_id_for(doi: str | None, url: str | None) -> str:
    if doi:
        return f"src:{doi.lower()}"
    assert url
    return "src:url:" + hashlib.sha256(_norm_url(url).encode()).hexdigest()[:12]


@dataclass
class SourceIndex:
    """Maps any citation string (DOI or URL) to one source record."""

    by_key: dict[str, Source] = field(default_factory=dict)

    @classmethod
    def build(cls, rows: Iterable[CitationRow]) -> SourceIndex:
        idx = cls()
        for r in rows:
            src = Source(
                id=source_id_for(r.doi, r.urls[0] if r.urls else None),
                created_by=ACTOR,
                doi=r.doi,
                url=r.urls[0] if r.urls else None,
                title=r.title or r.reference,
                authors=r.authors,
                year=r.year,
                venue=r.venue,
                checked=r.checked or None,
            )
            for k in [*(r.urls), *([r.doi] if r.doi else [])]:
                idx.by_key[k.lower()] = src
        return idx

    def resolve(self, citation: str) -> Source:
        key = _norm_url(citation).lower()
        if key in self.by_key:
            return self.by_key[key]
        doi = citation.lower() if _DOI.match(citation) else None
        src = Source(
            id=source_id_for(doi, None if doi else citation),
            created_by=ACTOR,
            doi=doi,
            url=None if doi else _norm_url(citation),
            title="(not listed in CITATIONS.md)",
        )
        self.by_key[key] = src
        return src


# --- curate run states --------------------------------------------------------------------


def _central(v: Any) -> tuple[float | None, float | None, float | None]:
    if isinstance(v, (list, tuple)):
        return None, float(v[0]), float(v[1])
    return float(v), None, None


def _close(a: float | None, b: float | None) -> bool:
    if a is None or b is None:
        return a is b
    return abs(a - b) <= 1e-9 * max(1.0, abs(a), abs(b))


@dataclass(frozen=True)
class RunMatch:
    run: str
    doi: str
    quote: str
    section: str
    mapping: str
    text: str
    sections: list[tuple[str, int]]
    fetched_from: str


def find_run_match(runs_dir: Path | None, path: str, value: Any) -> RunMatch | None:
    """A checked curate claim for ``path`` with the same value whose text we still have."""
    if runs_dir is None:
        return None
    clade, rel = path.split(".", 1)
    f = runs_dir / f"{clade}.json"
    if not f.exists():
        return None
    st = json.loads(f.read_text())
    v, lo, hi = _central(value)
    for doi, ex in sorted(st.get("extractions", {}).items()):
        for c in ex.get("claims", []):
            if c.get("path") != rel:
                continue
            if not (
                _close(c.get("value"), v)
                and _close(c.get("value_low"), lo)
                and _close(c.get("value_high"), hi)
            ):
                continue
            t = st.get("texts", {}).get(doi) or {}
            if not t.get("fulltext") or not t.get("text"):
                continue
            return RunMatch(
                run=f"curate:{clade}@{st.get('curated_at') or 'unknown'}",
                doi=doi,
                quote=str(c["quote"]),
                section=str(c.get("section", "")),
                mapping=str(c.get("mapping", "close")),
                text=normalise(str(t["text"])),
                sections=[(str(a), int(b)) for a, b in t.get("sections", [])],
                fetched_from=str(t.get("source", "")),
            )
    return None


# --- claims -------------------------------------------------------------------------------


def infer_method(conditions: str | None) -> str | None:
    c = (conditions or "").lower()
    if "behavio" in c or "audiogram" in c or "minimum audible" in c:
        return "behavioural"
    if "microscop" in c or "histolog" in c or "anatom" in c:
        return "anatomical"
    if "electro" in c or "search coil" in c or "recording" in c or "evoked" in c:
        return "electrophysiology"
    if "model" in c:
        return "model"
    return None


def quantity_of(value: Any, unit: str) -> Quantity:
    v, lo, hi = _central(value)
    q = Quantity(value=v, low=lo, high=hi, unit=unit)
    norm = literature.normalise_unit(unit)
    if norm is not None:
        canon, factor = norm
        q.canonical_unit = canon
        q.canonical_value = None if v is None else v * factor
        q.canonical_low = None if lo is None else lo * factor
        q.canonical_high = None if hi is None else hi * factor
    return q


def _unverified_reason(sources: list[Source]) -> str:
    checked = sorted({s.checked for s in sources if s.checked})
    if checked:
        return (
            "No full-text quote in the ledger yet. CITATIONS.md says the source was checked "
            f"as: {', '.join(checked)}."
        )
    return "No full-text quote in the ledger yet, and the source is not in CITATIONS.md."


@dataclass
class ImportResult:
    claims_created: list[str] = field(default_factory=list)
    claims_revised: list[str] = field(default_factory=list)
    claims_unchanged: list[str] = field(default_factory=list)
    hypotheses_created: list[str] = field(default_factory=list)
    sources: int = 0
    passages: int = 0

    def summary(self) -> str:
        return (
            f"{len(self.claims_created)} claims created, {len(self.claims_revised)} revised, "
            f"{len(self.claims_unchanged)} unchanged; {len(self.hypotheses_created)} new "
            f"hypotheses; {self.sources} sources, {self.passages} passages"
        )


def import_library(
    store: Store, profiles_root: Path, *, runs_dir: Path | None = None
) -> ImportResult:
    """Import library quantities as claims, and requests and open questions as hypotheses."""
    from senseai_profiles.library import load_library

    res = ImportResult()
    library = load_library(profiles_root / "library")
    index = SourceIndex.build(parse_citations(profiles_root / "library" / "CITATIONS.md"))
    seen_sources: set[str] = set()
    for key in sorted(library):
        clade = library[key]
        for q in clade.quantities():
            sources: list[Source] = []
            for cite in q.citations:
                s = index.resolve(cite)
                if s.id not in {x.id for x in sources}:
                    sources.append(s)
            for s in sources:
                if s.id not in seen_sources:
                    store.put_source(store.get("source", s.id, Source) or s, ACTOR)
                    seen_sources.add(s.id)
            value = list(q.value) if isinstance(q.value, tuple) else q.value
            match = find_run_match(runs_dir, q.path, value)
            passage_id = None
            status: ClaimStatus = "unverified"
            mapping: MappingKind | None = None
            provenance: Provenance = "library"
            if match is not None:
                passage_id = _passage_from_run(store, match, index)
                if passage_id is not None:
                    res.passages += 1
                    status, provenance = "extracted", "pipeline"
                    mapping = cast(MappingKind, match.mapping)
            fields: dict[str, Any] = {
                "library_path": q.path,
                "quantity": quantity_of(value, q.unit).model_dump(),
                "species": q.species,
                "conditions": q.conditions,
                "method": infer_method(q.conditions),
                "source_ids": [s.id for s in sources],
                "library_notes": q.notes,
                "library_confidence": q.confidence,
            }
            cid = f"lib:{q.path}"
            existing = store.get("claim", cid, Claim)
            if existing is None:
                store.create_claim(
                    Claim(
                        id=cid,
                        created_by=ACTOR,
                        passage_id=passage_id,
                        status=status,
                        mapping=mapping,
                        provenance=provenance,
                        unverified_reason=None if passage_id else _unverified_reason(sources),
                        **fields,
                    ),
                    ACTOR,
                )
                res.claims_created.append(cid)
                continue
            cur = existing.model_dump()
            changed = {k: v for k, v in fields.items() if cur.get(k) != v}
            if changed:
                store.revise_claim(
                    cid, changed, reason="the library value changed upstream", by=ACTOR
                )
                res.claims_revised.append(cid)
            else:
                res.claims_unchanged.append(cid)
    res.sources = len(seen_sources)
    res.hypotheses_created = import_hypotheses(store, profiles_root)
    return res


def _passage_from_run(store: Store, m: RunMatch, index: SourceIndex) -> str | None:
    loc = locate(m.text, m.quote, m.sections)
    if loc is None:
        return None
    src = index.resolve(m.doi)
    sha = store.store_text(m.text, m.sections)
    src = src.model_copy(
        update={
            "fulltext_available": True,
            "fetched_text_sha256": sha,
            "fetched_from": m.fetched_from or None,
            "retrieved_at": src.retrieved_at or now(),
        }
    )
    store.put_source(src, ACTOR)
    pid = (
        "psg-"
        + hashlib.sha256(f"{src.id}:{loc.char_start}:{loc.char_end}".encode()).hexdigest()[:16]
    )
    store.add_passage(
        Passage(
            id=pid,
            created_by=f"pipeline:{m.run}",
            source_id=src.id,
            char_start=loc.char_start,
            char_end=loc.char_end,
            text=loc.text,
            section=loc.section or m.section,
            context_before=loc.context_before,
            context_after=loc.context_after,
        ),
        ACTOR,
    )
    return pid


# --- hypotheses ---------------------------------------------------------------------------


def _open_questions(root: Path) -> list[tuple[str, int, str]]:
    """``(target, n, text)`` for every open question in the library, in file order."""
    out: list[tuple[str, int, str]] = []
    for p in sorted((root / "library").glob("*.yaml")):
        data = yaml.safe_load(p.read_text()) or {}
        key = p.stem
        for i, t in enumerate(data.get("open_questions") or []):
            out.append((key, i + 1, str(t)))
        for s in data.get("senses") or []:
            for i, t in enumerate(s.get("open_questions") or []):
                out.append((f"{key}.senses.{s['id']}", i + 1, str(t)))
        for b in data.get("behaviours") or []:
            for i, t in enumerate(b.get("open_questions") or []):
                out.append((f"{key}.behaviours.{b['name']}", i + 1, str(t)))
    return out


def _requests(root: Path) -> dict[str, dict[str, Any]]:
    from senseai_profiles.research import ResearchPaths, load_requests

    reqs: dict[str, dict[str, Any]] = load_requests(ResearchPaths(root))
    return reqs


def _linked(store_claims: Mapping[str, Claim], target: str) -> list[str]:
    """Claims a hypothesis is about: only when the target names one value path."""
    if len(target.split(".")) < 4:
        return []
    return sorted(c.id for c in store_claims.values() if c.library_path == target)


def import_hypotheses(store: Store, root: Path) -> list[str]:
    created: list[str] = []
    claims = {c.id: c for c in store.claims()}
    reqs = _requests(root)
    by_question = {
        normalise(r["question"]): r for r in reqs.values() if r.get("origin", "sync") == "sync"
    }
    for rid, r in sorted(reqs.items()):
        if r["status"] not in OPEN_REQUEST_STATUSES:
            continue
        hid = f"hyp:request:{rid}"
        if store.get_raw("hypothesis", hid) is not None:
            continue
        store.put_hypothesis(
            Hypothesis(
                id=hid,
                created_by=ACTOR,
                statement=r["question"],
                origin=f"request:{rid}",
                priority=r["why"]["priority"],
                target=r["target"],
                consumers=list(r["why"].get("consumers", [])),
                linked_claims=_linked(claims, r["target"]),
            ),
            ACTOR,
        )
        created.append(hid)
    for target, n, text in _open_questions(root):
        if normalise(text) in by_question:
            continue  # the synced request for this question is the hypothesis
        hid = f"hyp:open_question:{target}#{n}"
        if store.get_raw("hypothesis", hid) is not None:
            continue
        store.put_hypothesis(
            Hypothesis(
                id=hid,
                created_by=ACTOR,
                statement=text,
                origin=f"open_question:{target}#{n}",
                priority="medium",
                target=target,
            ),
            ACTOR,
        )
        created.append(hid)
    return created


__all__ = [
    "ACTOR",
    "CitationRow",
    "ImportResult",
    "SourceIndex",
    "find_run_match",
    "import_hypotheses",
    "import_library",
    "infer_method",
    "parse_citations",
    "quantity_of",
    "source_id_for",
]
