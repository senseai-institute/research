# SPDX-License-Identifier: Apache-2.0
"""Reconcile claims per path: agreement, confidence, primary-versus-review, citation chains.

Rules (from the design):

- values from >= 2 independent primary papers within tolerance -> ``high``;
- one primary paper, or several papers that all trace to one primary -> ``medium``;
- conflicting values -> ``low``, all values listed and a note;
- a review that cites a primary paper also in the run is folded into that
  paper's support rather than counted as independent;
- abstract-only and review claims never raise confidence above ``medium``.

Tolerance is relative (10 %) for ordinary units, absolute (0.15) for log
units and 10 % of the span for ranges.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from senseai_research.curate.extract import Claim

REL_TOL = 0.10
LOG_TOL = 0.15
LOG_UNITS = ("log10 re human",)


@dataclass
class Resolved:
    path: str
    value: float | tuple[float, float]
    unit: str
    confidence: str  # high | medium | low
    primary_doi: str  # the claim whose value is written
    supporting: list[Claim]  # claims that agree (including the primary)
    conflicting: list[Claim]
    note: str = ""
    independent: int = 0  # independent primary papers in agreement

    @property
    def citations(self) -> list[str]:
        out: list[str] = []
        for c in [*self.supporting, *self.conflicting]:
            if c.doi not in out:
                out.append(c.doi)
        return out


def _central(c: Claim) -> float | None:
    if c.canonical_value is not None:
        return c.canonical_value
    if c.canonical_low is not None and c.canonical_high is not None:
        return (c.canonical_low + c.canonical_high) / 2
    return None


def agree(a: Claim, b: Claim) -> bool:
    if a.canonical_unit != b.canonical_unit or a.canonical_unit is None:
        return False
    va, vb = _central(a), _central(b)
    if va is None or vb is None:
        return False
    if a.canonical_unit in LOG_UNITS:
        return abs(va - vb) <= LOG_TOL
    scale = max(abs(va), abs(vb), 1e-12)
    return abs(va - vb) / scale <= REL_TOL


def _rank(c: Claim) -> tuple[int, int, int]:
    """Lower sorts first: primary full text, exact mapping, fewer flags."""
    st = {"primary": 0, "review": 2, "abstract_only": 1}.get(c.source_type, 3)
    return (st, {"exact": 0, "close": 1, "loose": 2}.get(c.mapping, 3), len(c.flags))


def _chain_targets(c: Claim, chains: Mapping[str, Iterable[str]]) -> set[str]:
    """DOIs this (review) claim's paper cites, lower-cased."""
    return {d.lower() for d in chains.get(c.doi, ())}


def reconcile_path(
    path: str, claims: list[Claim], *, chains: Mapping[str, Iterable[str]] | None = None
) -> Resolved | None:
    """Resolve one path. ``chains`` maps a DOI to the DOIs it references (Crossref/OpenAlex)."""
    chains = chains or {}
    usable = [c for c in claims if c.path == path and _central(c) is not None]
    if not usable:
        return None
    usable.sort(key=_rank)
    # Fold reviews into the primaries they cite (dependent evidence).
    dois_present = {c.doi for c in usable}
    dependent: set[int] = set()
    for i, c in enumerate(usable):
        if c.source_type == "review" and _chain_targets(c, chains) & dois_present:
            dependent.add(i)
    # Cluster by agreement with the best-ranked claim.
    lead = usable[0]
    supporting = [c for c in usable if agree(lead, c)]
    conflicting = [c for c in usable if c not in supporting]
    independent = len(
        {
            c.doi
            for i, c in enumerate(usable)
            if c in supporting and i not in dependent and c.source_type == "primary"
        }
    )
    note = ""
    if conflicting:
        confidence = "low"
        vals = ", ".join(f"{_fmt(c)} ({c.doi})" for c in [lead, *conflicting])
        note = f"conflicting values across papers: {vals}"
    elif independent >= 2:
        confidence = "high"
    else:
        confidence = "medium"
        if lead.source_type != "primary":
            kind = lead.source_type.replace("_", " ")
            note = f"best evidence is {kind}; no primary measurement in the run"
    if "unit_unparsed" in lead.flags:
        confidence = "low"
        note = (note + "; " if note else "") + f"unit {lead.unit!r} not recognised"
    value: float | tuple[float, float]
    unit = lead.canonical_unit or lead.unit
    if lead.canonical_value is not None:
        value = lead.canonical_value
    elif lead.canonical_low is not None and lead.canonical_high is not None:
        value = (lead.canonical_low, lead.canonical_high)
    else:  # unit unparsed: keep the paper's numbers
        value = (
            lead.value
            if lead.value is not None
            else (lead.value_low or 0.0, lead.value_high or 0.0)
        )
    return Resolved(
        path, value, unit, confidence, lead.doi, supporting, conflicting, note, independent
    )


def _fmt(c: Claim) -> str:
    v = _central(c)
    return f"{v:g} {c.canonical_unit or c.unit}" if v is not None else "?"


@dataclass
class Reconciliation:
    resolved: dict[str, Resolved] = field(default_factory=dict)
    unresolved_paths: list[str] = field(default_factory=list)

    def counts(self) -> dict[str, int]:
        out = {"high": 0, "medium": 0, "low": 0}
        for r in self.resolved.values():
            out[r.confidence] += 1
        return out


def reconcile(
    claims: Iterable[Claim],
    paths: Iterable[str],
    *,
    chains: Mapping[str, Iterable[str]] | None = None,
) -> Reconciliation:
    cl = list(claims)
    rec = Reconciliation()
    for p in paths:
        r = reconcile_path(p, cl, chains=chains)
        if r is None:
            rec.unresolved_paths.append(p)
        else:
            rec.resolved[p] = r
    return rec


__all__ = [
    "LOG_TOL",
    "REL_TOL",
    "Reconciliation",
    "Resolved",
    "agree",
    "reconcile",
    "reconcile_path",
]
