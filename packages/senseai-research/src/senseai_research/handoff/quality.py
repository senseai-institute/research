# SPDX-License-Identifier: Apache-2.0
"""How far a claim can be trusted as a model input: a score from 0 to 1 with its reasons.

The score is a product of factors, each a number at most 1 with one plain sentence, so a reader
sees what cost a claim its trust and by how much (docs/HANDOFF.md §Quality). Hard gates remove
a claim outright: rejected or superseded, a retracted source, no source at all, or a value that
cannot be put in the knob's unit.

The factors are a starting point written down so they can be argued with, not a fitted model.
Changing one changes every package built after it; ``QUALITY_VERSION`` is in each package.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Literal

from pydantic import BaseModel, ConfigDict

from senseai_research.handoff.rules import Rule
from senseai_research.ledger.models import Claim, Source

QUALITY_VERSION = 1

Label = Literal["high", "medium", "low"]

STATUS: dict[str, tuple[float, str]] = {
    "verified": (1.0, "a person checked it against the paper's text"),
    "extracted": (0.8, "its quote was found in the paper's text, not yet checked by a person"),
    "disputed": (0.45, "it is disputed"),
    "unverified": (0.55, "no quote from the paper backs it in the ledger yet"),
}
MAPPING: dict[str, tuple[float, str]] = {
    "exact": (1.0, "the paper measures exactly this quantity"),
    "close": (0.9, "the paper measures something close to this quantity"),
    "loose": (0.65, "the paper's measurement only loosely matches this quantity"),
}
CONFIDENCE: dict[str, tuple[float, str]] = {
    "high": (1.0, "the library's authors rate it high confidence"),
    "medium": (0.9, "the library's authors rate it medium confidence"),
    "low": (0.7, "the library's authors rate it low confidence"),
}
INDIRECT_METHOD: dict[str, tuple[float, str]] = {
    "review": (0.75, "it comes from a review, not the original measurement"),
    "model": (0.65, "it comes from a model, not a measurement"),
}
FLAG_PENALTY = 0.8
HIGH, MEDIUM = 0.7, 0.45


class Factor(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    multiplier: float
    sentence: str


class ClaimQuality(BaseModel):
    model_config = ConfigDict(extra="forbid")

    claim_id: str
    score: float  # 0 when gated
    label: Label | None  # None when gated
    gate: str | None = None  # why the claim cannot be used at all
    factors: list[Factor]

    @property
    def usable(self) -> bool:
        return self.gate is None


def label(score: float) -> Label:
    return "high" if score >= HIGH else "medium" if score >= MEDIUM else "low"


def _species_names(representative: Sequence[str]) -> set[str]:
    return {s.strip().lower() for s in representative if s.strip()}


def score(
    claim: Claim,
    sources: Sequence[Source],
    *,
    rule: Rule | None = None,
    representative: Sequence[str] = (),
) -> ClaimQuality:
    """Score one claim for use through ``rule`` (``None``: as evidence in general)."""
    gate: str | None = None
    if claim.status in ("rejected", "superseded"):
        gate = f"the claim was {claim.status} by a person"
    elif not sources:
        gate = "it cites no source the ledger knows"
    elif retracted := [s for s in sources if s.vetting and s.vetting.retracted]:
        gate = f"its source {retracted[0].doi or retracted[0].url} was retracted"
    if gate:
        return ClaimQuality(claim_id=claim.id, score=0.0, label=None, gate=gate, factors=[])

    f: list[Factor] = []

    def add(name: str, m: float, sentence: str) -> None:
        f.append(Factor(name=name, multiplier=round(m, 4), sentence=sentence))

    m, s = STATUS[claim.status]
    add("status", m, s[0].upper() + s[1:] + ".")

    if claim.passage_id:
        add("reading", 1.0, "The supporting passage is stored verbatim.")
    elif any(s.fulltext_available for s in sources):
        add("reading", 0.95, "The full text is available but no passage is stored.")
    elif any(s.checked and "full" in s.checked.lower() for s in sources):
        add("reading", 0.95, "The library's citation table says the full paper was read.")
    else:
        add("reading", 0.85, "Only the abstract is known to have been read.")

    if claim.mapping:
        m, s = MAPPING[claim.mapping]
        add("mapping", m, s[0].upper() + s[1:] + ".")

    if claim.method in INDIRECT_METHOD:
        m, s = INDIRECT_METHOD[claim.method]
        add("method", m, s[0].upper() + s[1:] + ".")
    elif rule is not None and claim.method and claim.method not in rule.methods:
        add(
            "method",
            0.85,
            f"It was measured by {claim.method} means; this knob is usually set from "
            f"{' or '.join(rule.methods)} data.",
        )
    elif claim.method is None:
        add("method", 0.95, "The method is not recorded.")

    names = _species_names(representative)
    if claim.species and names and claim.species.strip().lower() not in names:
        add(
            "species",
            0.85,
            f"It was measured in {claim.species}, not a representative species of the clade.",
        )
    elif not claim.species:
        add("species", 0.85, "The species it was measured in is not recorded.")

    if claim.library_confidence in CONFIDENCE:
        m, s = CONFIDENCE[claim.library_confidence]
        if m < 1.0:
            add("library_confidence", m, s[0].upper() + s[1:] + ".")

    for src in sources:
        v = src.vetting
        if v is None:
            continue
        for flag in v.flags:
            add("vetting", FLAG_PENALTY, f"Its source is flagged: {flag}.")
        if v.crossref_match is False:
            add("vetting", FLAG_PENALTY, "Its source's details do not match Crossref.")

    total = 1.0
    for x in f:
        total *= x.multiplier
    total = round(total, 4)
    return ClaimQuality(claim_id=claim.id, score=total, label=label(total), factors=f)


__all__ = ["QUALITY_VERSION", "ClaimQuality", "Factor", "Label", "label", "score"]
