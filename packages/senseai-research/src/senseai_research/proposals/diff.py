# SPDX-License-Identifier: Apache-2.0
"""Build the YAML patch to ``profiles/library/<clade>.yaml`` from approved claims (spec §7.4).

For each claim: the value, its conditions and species when they changed, the citation of the
source the quote came from, and an ``evidence`` item with the verbatim quote and where it is in
the paper. The edit is a ruamel round-trip, so comments and layout are kept, and the patched
entry must still pass the library's own validation.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from senseai_research import libyaml
from senseai_research.impact.consumers import ProfilesTree
from senseai_research.ledger.models import Claim, Passage, Source
from senseai_research.ledger.store import Store

EVIDENCE_QUOTE_MAX = 300  # library schema: evidence_item.quote maxLength


class PatchError(ValueError):
    pass


@dataclass
class Patch:
    file: str  # repo-relative, e.g. library/felidae.yaml
    old_text: str
    new_text: str
    diff: str
    title: str
    rows: list[dict[str, Any]] = field(default_factory=list)
    old_values: dict[str, Any] = field(default_factory=dict)


def _fmt(v: Any) -> str:
    if isinstance(v, list):
        return f"{v[0]:g} to {v[1]:g}"
    return f"{v:g}" if isinstance(v, (int, float)) else str(v)


def _citation(src: Source) -> str:
    if src.doi:
        return src.doi
    if src.url:
        return src.url
    raise PatchError(f"source {src.id} has neither a DOI nor a URL")


def build_patch(store: Store, profiles_root: Path, claims: Sequence[Claim]) -> Patch:
    if not claims:
        raise PatchError("no claims to propose")
    tree = ProfilesTree(profiles_root)
    files = {tree.library_file(c.library_path) for c in claims}
    if len(files) != 1:
        raise PatchError("one proposal changes one library file")
    (f,) = files
    old = f.read_text()
    text = old
    rows: list[dict[str, Any]] = []
    old_values: dict[str, Any] = {}
    for c in claims:
        if c.passage_id is None:
            raise PatchError(f"{c.library_path} has no passage (N2)")
        p = store.must("passage", c.passage_id, Passage)
        src = store.must("source", p.source_id, Source)
        cur = libyaml.get_quantity(text, c.library_path)
        if cur.get("unit") != c.quantity.unit:
            raise PatchError(
                f"{c.library_path}: the claim is in {c.quantity.unit!r}, the library in "
                f"{cur.get('unit')!r}"
            )
        cite = _citation(src)
        citations = list(cur.get("citations") or [])
        if cite not in citations:
            citations.append(cite)
        quote = p.text if len(p.text) <= EVIDENCE_QUOTE_MAX else p.text[:EVIDENCE_QUOTE_MAX]
        item: dict[str, Any] = {
            "doi": cite,
            "quote": quote,
            "page_or_section": p.section or p.page or p.table or p.figure or "full text",
            "source_type": "primary",
            "fulltext": bool(src.fulltext_available),
            "extracted_by": "senseai-research (reviewed by a person)",
        }
        if c.conditions:
            item["conditions"] = c.conditions
        evidence = [e for e in (cur.get("evidence") or []) if e.get("quote") != quote]
        changes: dict[str, Any] = {
            "value": c.quantity.library_value(),
            "citations": citations,
            "evidence": [*evidence, item],
        }
        if c.conditions and c.conditions != cur.get("conditions"):
            changes["conditions"] = c.conditions
        if c.species and c.species != cur.get("species"):
            changes["species"] = c.species
        old_values[c.library_path] = cur.get("value")
        text = libyaml.set_quantity(text, c.library_path, changes)
        rows.append(
            {
                "label": c.library_path,
                "before": f"{_fmt(cur.get('value'))} {cur.get('unit')}",
                "after": c.quantity.text(),
            }
        )
    _validate(tree, f, text)
    rel = f.relative_to(profiles_root).as_posix()
    clade = f.stem
    first = claims[0]
    path = first.library_path.split(".", 1)[1]
    title = (
        f"library({clade}): {path} {_fmt(old_values[first.library_path])} → "
        f"{_fmt(first.quantity.library_value())}"
    )
    if len(claims) > 1:
        title += f" (+{len(claims) - 1} more)"
    return Patch(rel, old, text, libyaml.unified_diff(old, text, rel), title, rows, old_values)


def _validate(tree: ProfilesTree, f: Path, text: str) -> None:
    try:
        import yaml
        from senseai_profiles.library import validate_entry
    except ImportError:  # validation is profiles' own; without it, profiles CI still checks
        return
    try:
        validate_entry(yaml.safe_load(text), file=f, schema_path=tree.schema)
    except ValueError as e:
        raise PatchError(f"the patched entry would not validate: {e}") from e


__all__ = ["EVIDENCE_QUOTE_MAX", "Patch", "PatchError", "build_patch"]
