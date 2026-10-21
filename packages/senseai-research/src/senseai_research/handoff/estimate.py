# SPDX-License-Identifier: Apache-2.0
"""From the claims behind one knob to one estimate: a value, an interval, and a readiness.

Each usable claim is normalised to the rule's unit and put through the rule's transform. The
value is the quality-weighted mean of the claims' central values; the interval spans every
claim's own range (or point), in knob units. Readiness says how much a model builder should
lean on the value (docs/HANDOFF.md §Readiness):

``ready``
    at least two independent sources agree, and the best claim is high quality;
``provisional``
    at least one usable claim, but fewer sources, lower quality, or disagreement;
``insufficient``
    no usable claim.

``strength`` (0-1) folds the same facts into one number for a prior's weight: the best claim's
score, scaled by how many independent sources there are, halved when they disagree.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Literal

from pydantic import BaseModel, ConfigDict

from senseai_research.curate.units import normalise_value
from senseai_research.handoff.quality import HIGH, ClaimQuality
from senseai_research.handoff.rules import Knob, Rule
from senseai_research.ledger.models import Claim

Readiness = Literal["ready", "provisional", "insufficient"]
READINESS_ORDER: dict[str, int] = {"insufficient": 0, "provisional": 1, "ready": 2}
#: Relative spread of the claims' central values above which they disagree.
AGREE_SPREAD = 0.25
SOURCE_FACTOR = {1: 0.7, 2: 0.9}  # 3 or more independent sources: 1.0


class Evidence(BaseModel):
    """One claim as used for one knob."""

    model_config = ConfigDict(extra="forbid")

    claim_id: str
    library_path: str
    as_stated: str  # the claim's value with its unit, as in the ledger
    knob_value: float | None  # through the rule; None when the claim is not usable here
    knob_low: float | None = None
    knob_high: float | None = None
    status: str
    species: str | None
    method: str | None
    source_ids: list[str]
    quality: ClaimQuality
    excluded: str | None = None  # why it is not in the estimate


class Estimate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    knob: str
    unit: str
    value: float | None
    low: float | None
    high: float | None
    readiness: Readiness
    strength: float
    agreement: Literal["agree", "disagree", "single", "none"]
    independent_sources: int
    in_range: bool
    sentences: list[str]
    evidence: list[Evidence]


def _central(claim: Claim) -> tuple[float, float, float] | None:
    """(value, low, high) in the claim's own unit."""
    q = claim.quantity
    if q.value is not None:
        lo = q.low if q.low is not None else q.value
        hi = q.high if q.high is not None else q.value
        return q.value, lo, hi
    if q.low is not None and q.high is not None:
        return (q.low + q.high) / 2.0, q.low, q.high
    return None


def _to_knob(claim: Claim, rule: Rule) -> tuple[float, float, float] | str:
    c = _central(claim)
    if c is None:
        return "it has no value"
    out = []
    for x in c:
        n = normalise_value(x, claim.quantity.unit)
        if n is None or n[1] != rule.unit:
            return f"its unit {claim.quantity.unit!r} cannot be put in {rule.unit}"
        try:
            out.append(rule.apply(n[0]))
        except (ZeroDivisionError, ValueError, OverflowError):
            return f"{x:g} {claim.quantity.unit} has no value under {rule.formula}"
    v, a, b = out
    return v, min(a, b), max(a, b)


def evidence_for(claim: Claim, quality: ClaimQuality, rule: Rule) -> Evidence:
    base = {
        "claim_id": claim.id,
        "library_path": claim.library_path,
        "as_stated": claim.quantity.text(),
        "status": claim.status,
        "species": claim.species,
        "method": claim.method,
        "source_ids": list(claim.source_ids),
        "quality": quality,
    }
    if quality.gate:
        return Evidence(knob_value=None, excluded=quality.gate, **base)
    conv = _to_knob(claim, rule)
    if isinstance(conv, str):
        return Evidence(knob_value=None, excluded=conv, **base)
    v, lo, hi = conv
    return Evidence(knob_value=v, knob_low=lo, knob_high=hi, **base)


def _fmt(x: float) -> str:
    return f"{x:.4g}"


def pool(knob: Knob, rule: Rule, evidence: Sequence[Evidence]) -> Estimate:
    used = [e for e in evidence if e.excluded is None and e.knob_value is not None]
    if not used:
        why = (
            f"No usable claim at {rule.leaf} yet."
            if not evidence
            else "No claim at this path can be used: "
            + "; ".join(f"{e.claim_id}: {e.excluded}" for e in evidence)
            + "."
        )
        return Estimate(
            knob=knob.name,
            unit=knob.unit,
            value=None,
            low=None,
            high=None,
            readiness="insufficient",
            strength=0.0,
            agreement="none",
            independent_sources=0,
            in_range=False,
            sentences=[why],
            evidence=list(evidence),
        )
    weights = [max(e.quality.score, 1e-6) for e in used]
    vals = [float(e.knob_value) for e in used if e.knob_value is not None]
    value = sum(w * v for w, v in zip(weights, vals, strict=True)) / sum(weights)
    low = min(float(e.knob_low if e.knob_low is not None else e.knob_value or 0) for e in used)
    high = max(float(e.knob_high if e.knob_high is not None else e.knob_value or 0) for e in used)
    sources = {s for e in used for s in e.source_ids}
    n_src = len(sources)
    best = max(e.quality.score for e in used)
    if len(used) == 1:
        agreement: Literal["agree", "disagree", "single", "none"] = "single"
    else:
        spread = (max(vals) - min(vals)) / max(abs(value), 1e-12)
        agreement = "agree" if spread <= AGREE_SPREAD else "disagree"
    in_range = knob.low <= value <= knob.high
    if n_src >= 2 and agreement == "agree" and best >= HIGH and in_range:
        readiness: Readiness = "ready"
    elif in_range:
        readiness = "provisional"
    else:
        readiness = "insufficient"
    strength = best * SOURCE_FACTOR.get(n_src, 1.0) * (0.5 if agreement == "disagree" else 1.0)
    strength = round(strength if in_range else 0.0, 4)

    s = [
        f"{_fmt(value)} {knob.unit} from {len(used)} claim(s) in {n_src} independent "
        f"source(s), via {rule.formula}."
    ]
    if high > low:
        s.append(f"The claims span {_fmt(low)} to {_fmt(high)} {knob.unit}.")
    if agreement == "disagree":
        s.append(
            f"The claims disagree by more than {AGREE_SPREAD:.0%}; resolve that before "
            "relying on the value."
        )
    if n_src < 2:
        s.append("Only one independent source supports it; a second would make it ready.")
    if best < HIGH:
        s.append(
            f"The best claim scores {best:.2f} (needs {HIGH:.2f} to be ready): "
            + " ".join(
                x.sentence
                for x in max(used, key=lambda e: e.quality.score).quality.factors
                if x.multiplier < 1.0
            )
        )
    if not in_range:
        s.append(
            f"{_fmt(value)} {knob.unit} is outside the range circuits accepts "
            f"({_fmt(knob.low)} to {_fmt(knob.high)}), so it is not passed on."
        )
    return Estimate(
        knob=knob.name,
        unit=knob.unit,
        value=round(value, 6),
        low=round(low, 6),
        high=round(high, 6),
        readiness=readiness,
        strength=strength,
        agreement=agreement,
        independent_sources=n_src,
        in_range=in_range,
        sentences=s,
        evidence=list(evidence),
    )


__all__ = ["READINESS_ORDER", "Estimate", "Evidence", "Readiness", "evidence_for", "pool"]
