# SPDX-License-Identifier: Apache-2.0
"""Assemble reconciled values into a draft ``library/<clade>.yaml``.

Merge policy against an existing (hand-written or earlier-draft) entry:

- a path that already carries a ``reviewer_note`` is never touched;
- an existing value that agrees with the curated one keeps its value and
  gains the ``evidence`` items and citations;
- an existing value that disagrees is replaced, the old value is recorded in
  ``notes`` and confidence drops to ``low`` so the sheet demands a decision;
- a new path is created, including placeholder senses/behaviours whose
  descriptive fields the reviewer must fill (they are listed in the sheet).

Every quantity written here carries ``evidence`` with a verbatim quote; the
schema's cross-field check in ``library.validate_entry`` refuses evidence
whose DOI is not among the quantity's citations.
"""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from senseai_profiles.library import validate_entry

from senseai_research.curate import CURATOR
from senseai_research.curate.catalogue import Catalogue
from senseai_research.curate.extract import Claim
from senseai_research.curate.reconcile import LOG_TOL, LOG_UNITS, REL_TOL, Reconciliation, Resolved

_CITATION = re.compile(r"^(10\.\d{4,9}/\S+|https?://\S+)$")
_HEADER = (
    "# SPDX-License-Identifier: CC-BY-4.0\n"
    "# Species Sensory Library — {clade}.\n"
    "# Describes the animal. It makes no claim about any machine built from it.\n"
    "# Draft assembled by {curator}; every curated value carries `evidence` with a\n"
    "# verbatim quote. Review with `senseai-library review` before relying on it.\n"
)


@dataclass
class MergeLog:
    written: list[str] = field(default_factory=list)
    kept: list[str] = field(default_factory=list)  # existing value agreed; evidence added
    replaced: list[tuple[str, Any, Any]] = field(default_factory=list)  # path, old, new
    skipped: list[str] = field(default_factory=list)  # reviewer_note present
    placeholders: list[str] = field(default_factory=list)  # senses/behaviours needing text


def evidence_item(c: Claim) -> dict[str, Any]:
    item: dict[str, Any] = {
        "doi": c.doi,
        "page_or_section": c.section or ("abstract" if not c.fulltext else "full text"),
        "quote": c.quote[:300],
        "source_type": c.source_type,
        "fulltext": c.fulltext,
        "extracted_by": CURATOR,
    }
    if c.conditions:
        item["conditions"] = c.conditions
    return item


def quantity_for(r: Resolved, *, default_species: str | None) -> dict[str, Any]:
    lead = next(c for c in r.supporting if c.doi == r.primary_doi)
    value: Any = list(r.value) if isinstance(r.value, tuple) else r.value
    q: dict[str, Any] = {
        "value": value,
        "unit": r.unit,
        "confidence": r.confidence,
    }
    species = lead.species or default_species
    if species:
        q["species"] = species
    if lead.conditions:
        q["conditions"] = lead.conditions
    if r.note:
        q["notes"] = r.note
    q["citations"] = [d for d in r.citations if _CITATION.match(d)]
    q["evidence"] = [evidence_item(c) for c in [*r.supporting, *r.conflicting]]
    return q


def _agrees(existing: Any, new: Any, unit: str) -> bool:
    def mid(v: Any) -> float | None:
        if isinstance(v, int | float):
            return float(v)
        if isinstance(v, list | tuple) and len(v) == 2:
            return (float(v[0]) + float(v[1])) / 2
        return None

    a, b = mid(existing), mid(new)
    if a is None or b is None:
        return False
    if unit in LOG_UNITS:
        return abs(abs(a) - abs(b)) <= LOG_TOL  # sign convention differs between entry and papers
    return abs(a - b) / max(abs(a), abs(b), 1e-12) <= REL_TOL


def _merge_quantity(
    old: dict[str, Any] | None, new: dict[str, Any], path: str, log: MergeLog
) -> dict[str, Any]:
    if old is None:
        log.written.append(path)
        return new
    if old.get("reviewer_note"):
        log.skipped.append(path)
        return old
    if old.get("unit") == new["unit"] and _agrees(old.get("value"), new["value"], new["unit"]):
        merged = dict(old)
        merged["citations"] = list(dict.fromkeys([*old.get("citations", []), *new["citations"]]))
        merged["evidence"] = [*old.get("evidence", []), *new["evidence"]]
        if new.get("notes") and new["notes"] not in str(old.get("notes", "")):
            merged["notes"] = (str(old.get("notes", "")).strip() + " " + new["notes"]).strip()
        log.kept.append(path)
        return merged
    log.replaced.append((path, old.get("value"), new["value"]))
    out = dict(new)
    out["confidence"] = "low"
    prev = (
        f"Replaced earlier value {old.get('value')} {old.get('unit')} "
        f"(confidence {old.get('confidence')})."
    )
    out["notes"] = (prev + " " + out.get("notes", "")).strip()
    if old.get("notes"):
        out["notes"] += " Earlier note: " + str(old["notes"]).strip()
    return out


def dict_of(x: Any) -> dict[str, Any]:
    """Type-narrow a mapping that YAML gave us (the same object, not a copy)."""
    assert isinstance(x, dict)
    return x


def _sense(entry: dict[str, Any], sense_id: str, log: MergeLog) -> dict[str, Any]:
    for s in entry.setdefault("senses", []):
        if s.get("id") == sense_id:
            return dict_of(s)
    s = {
        "id": sense_id,
        "modality": sense_id,
        "organ": "TODO (reviewer): organ",
        "signal": "TODO (reviewer): signal",
        "citations": [],
    }
    entry["senses"].append(s)
    log.placeholders.append(f"senses.{sense_id}")
    return s


def _behaviour(
    entry: dict[str, Any], name: str, catalogue: Catalogue, log: MergeLog
) -> dict[str, Any]:
    for b in entry.setdefault("behaviours", []):
        if b.get("name") == name:
            return dict_of(b)
    spec = catalogue.by_path().get(f"behaviours.{name}.latency_ms")
    uses = [s["id"] for s in entry.get("senses", [])][:1] or ["vision"]
    b = {
        "name": name,
        "uses_senses": uses,
        "description": spec.description if spec else f"TODO (reviewer): describe {name}",
        "citations": [],
    }
    entry["behaviours"].append(b)
    log.placeholders.append(f"behaviours.{name}")
    return b


def set_path(
    entry: dict[str, Any], path: str, q: dict[str, Any], catalogue: Catalogue, log: MergeLog
) -> None:
    parts = path.split(".")
    if parts[0] == "senses" and len(parts) == 3:
        sense = _sense(entry, parts[1], log)
        sense[parts[2]] = _merge_quantity(sense.get(parts[2]), q, path, log)
        _add_citations(sense, q["citations"])
    elif parts[0] == "senses" and len(parts) == 4 and parts[2] == "measurements":
        sense = _sense(entry, parts[1], log)
        items = sense.setdefault("measurements", [])
        old = next((m for m in items if m.get("id") == parts[3]), None)
        merged = _merge_quantity(old, q, path, log)
        spec = catalogue.by_path().get(path)
        merged.setdefault("id", parts[3])
        merged.setdefault("description", spec.description.split(": ", 1)[-1] if spec else parts[3])
        merged = {"id": merged.pop("id"), "description": merged.pop("description"), **merged}
        if old is None:
            items.append(merged)
        else:
            items[items.index(old)] = merged
        _add_citations(sense, q["citations"])
    elif parts[0] == "behaviours" and len(parts) == 3:
        b = _behaviour(entry, parts[1], catalogue, log)
        b[parts[2]] = _merge_quantity(b.get(parts[2]), q, path, log)
        _add_citations(b, q["citations"])
    else:
        raise ValueError(f"cannot place path {path!r} in an entry")


def _add_citations(section: dict[str, Any], dois: Iterable[str]) -> None:
    cites = section.setdefault("citations", [])
    for d in dois:
        if d not in cites:
            cites.append(d)


def new_entry(
    clade: str, rank: str, common_names: Iterable[str], species: Iterable[Mapping[str, Any]]
) -> dict[str, Any]:
    return {
        "clade": clade,
        "rank": rank,
        "common_names": list(common_names) or [clade.lower()],
        "representative_species": [dict(s) for s in species],
        "conservation": {"iucn": "mixed", "source": "https://www.iucnredlist.org/"},
        "senses": [],
        "behaviours": [],
        "contributors": [],
        "version": "0.1.0",
        "review_status": "draft",
    }


def assemble(
    entry: dict[str, Any] | None,
    rec: Reconciliation,
    catalogue: Catalogue,
    *,
    clade: str,
    rank: str = "family",
    common_names: Iterable[str] = (),
    species: Iterable[Mapping[str, Any]] = (),
    default_species: str | None = None,
    queries: Iterable[str] = (),
    cache_digest: str = "",
    papers_screened: int = 0,
    claims_extracted: int = 0,
    now: dt.datetime | None = None,
) -> tuple[dict[str, Any], MergeLog]:
    """Return the draft entry (validated against the schema) and what was merged."""
    data = dict(entry) if entry else new_entry(clade, rank, common_names, species)
    data = yaml.safe_load(yaml.safe_dump(data))  # deep copy via YAML round-trip
    log = MergeLog()
    if not default_species and data.get("representative_species"):
        default_species = data["representative_species"][0].get("name")
    for path in [p.path for p in catalogue.paths]:
        r = rec.resolved.get(path)
        if r is None:
            continue
        set_path(data, path, quantity_for(r, default_species=default_species), catalogue, log)
    stamp = (now or dt.datetime.now(dt.UTC)).replace(microsecond=0).isoformat()
    data["curated_by"] = CURATOR
    data["curated_at"] = stamp
    data["reviewed_by"] = None
    data["reviewed_at"] = None
    data["review_status"] = "draft"
    contributors = data.setdefault("contributors", [])
    if not any(c.get("role") == "drafted-by-ai" for c in contributors):
        contributors.append({"name": CURATOR, "role": "drafted-by-ai"})
    data["curation"] = {
        "queries": list(queries),
        "cache_digest": cache_digest,
        "empty_paths": list(rec.unresolved_paths),
        "papers_screened": papers_screened,
        "claims_extracted": claims_extracted,
    }
    validate_entry(data, file=None)
    return data, log


def dump_entry(data: Mapping[str, Any], path: Path) -> None:
    body = yaml.safe_dump(dict(data), sort_keys=False, allow_unicode=True, width=100)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_HEADER.format(clade=data["clade"], curator=CURATOR) + "\n" + body)


def load_entry_data(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    with path.open() as f:
        out = yaml.safe_load(f)
    return dict(out) if out else None


__all__ = [
    "MergeLog",
    "assemble",
    "dump_entry",
    "evidence_item",
    "load_entry_data",
    "new_entry",
    "quantity_for",
    "set_path",
]
