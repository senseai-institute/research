# SPDX-License-Identifier: Apache-2.0
"""The handoff package: everything circuits needs to build a clade's model from the literature.

``circuits`` at the top level holds ``{knob: value}`` for the knobs research can support, so
the package is itself a valid bundle ``meta`` for ``CircuitParams.from_bundle_meta`` and its
``circuits`` section drops into a profile spec unchanged. Next to it, ``knobs`` carries every
knob's estimate, interval, readiness and evidence, and ``gaps`` lists what is missing and what
to measure (docs/HANDOFF.md).

The package is a pure function of the ledger, the profiles checkout and this code: the same
inputs give the same bytes and the same ``digest``. No clock time is inside it; the release
record (``HandoffRelease``) holds who released it and when.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

from senseai_research import __version__
from senseai_research.config import Settings
from senseai_research.handoff import rules
from senseai_research.handoff.estimate import (
    READINESS_ORDER,
    Estimate,
    Readiness,
    evidence_for,
    pool,
)
from senseai_research.handoff.quality import QUALITY_VERSION, score
from senseai_research.impact import git_sha
from senseai_research.ledger.models import Claim, Source
from senseai_research.ledger.store import Store

FORMAT = "senseai-research/handoff@1"
Value = float | int | bool

GapKind = Literal["no_evidence", "no_literature", "out_of_range", "below_policy", "disagree"]


class Policy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: the least readiness a value needs to go into ``circuits``
    min_readiness: Literal["ready", "provisional"] = "provisional"


class KnobEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    knob: str
    kind: str  # physics | measured | proposed (the circuits registry)
    unit: str
    default: Value
    range: tuple[float, float]
    rule: dict[str, Any] | None
    estimate: Estimate | None
    passed: bool  # whether ``circuits`` carries a value for it
    sentence: str


class Gap(BaseModel):
    model_config = ConfigDict(extra="forbid")

    knob: str
    kind: GapKind
    sentence: str
    ask: str  # what a contributor could do about it


class SourceRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    doi: str | None
    url: str | None
    title: str
    authors: str
    year: int | None
    venue: str


class Provenance(BaseModel):
    model_config = ConfigDict(extra="forbid")

    research_version: str
    research_sha: str
    profiles_sha: str
    circuits_registry_sha: str
    quality_version: int


class Handoff(BaseModel):
    model_config = ConfigDict(extra="forbid")

    format: str = FORMAT
    clade: str
    representative_species: list[str]
    policy: Policy
    circuits: dict[str, Value]
    summary: list[str]
    knobs: list[KnobEntry]
    gaps: list[Gap]
    engineering_knobs: list[str] = Field(
        description="physics knobs: set by the circuit design, not by the literature"
    )
    sources: dict[str, SourceRef]
    provenance: Provenance
    digest: str = ""

    def body(self) -> dict[str, Any]:
        d = self.model_dump(mode="json")
        d.pop("digest", None)
        return d

    def compute_digest(self) -> str:
        return hashlib.sha256(canonical(self.body()).encode()).hexdigest()

    def text(self) -> str:
        """The file circuits reads: canonical JSON with the digest."""
        return json.dumps(self.model_dump(mode="json"), indent=1, sort_keys=True) + "\n"


def canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


class HandoffError(ValueError):
    pass


def representative_species(cfg: Settings, clade: str) -> list[str]:
    if cfg.profiles_repo is None:
        return []
    f = cfg.profiles_repo / "library" / f"{clade}.yaml"
    if not f.is_file():
        return []
    doc = yaml.safe_load(f.read_text()) or {}
    return [str(s["name"]) for s in doc.get("representative_species") or [] if s.get("name")]


def clades(store: Store) -> list[str]:
    return sorted({c.clade for c in store.claims()})


def _research_sha() -> str:
    import os

    return os.environ.get("RESEARCH_GIT_SHA") or git_sha(Path(__file__).resolve().parent)


def _coerce(knob: rules.Knob, v: float) -> Value:
    if knob.type == "int":
        return round(v)
    return round(v, 6)


def build(store: Store, cfg: Settings, clade: str, *, policy: Policy | None = None) -> Handoff:
    """The package for ``clade`` from the ledger as it is now."""
    clade = clade.lower()
    policy = policy or Policy()
    all_claims = [c for c in store.claims() if c.clade == clade]
    if not all_claims:
        raise HandoffError(f"the ledger has no claims for {clade!r}")
    reps = representative_species(cfg, clade)
    by_path: dict[str, list[Claim]] = {}
    for c in all_claims:
        by_path.setdefault(c.library_path, []).append(c)
    reg = rules.knobs()
    by_knob = rules.rules_by_knob()
    need = READINESS_ORDER[policy.min_readiness]

    entries: list[KnobEntry] = []
    gaps: list[Gap] = []
    circuits: dict[str, Value] = {}
    cited: set[str] = set()
    for name in sorted(reg):
        k = reg[name]
        common = {
            "knob": name,
            "kind": k.kind,
            "unit": k.unit,
            "default": k.default,
            "range": (k.low, k.high),
        }
        if k.kind == "physics":
            continue
        rule = by_knob.get(name)
        if rule is None:
            why = rules.NO_LITERATURE.get(name, "no rule maps a library measurement to it")
            entries.append(
                KnobEntry(
                    rule=None,
                    estimate=None,
                    passed=False,
                    sentence=f"Not set from the literature: {why}. Circuits keeps its default "
                    f"{k.default} {k.unit}.",
                    **common,
                )
            )
            gaps.append(
                Gap(
                    knob=name,
                    kind="no_literature",
                    sentence=f"{name} is not a published quantity: {why}.",
                    ask="Fit it in an experiment (senseai-bench) and record the result.",
                )
            )
            continue
        path = f"{clade}.{rule.leaf}"
        ev = []
        for c in by_path.get(path, []):
            srcs = [s for sid in c.source_ids if (s := store.get("source", sid, Source))]
            q = score(c, srcs, rule=rule, representative=reps)
            ev.append(evidence_for(c, q, rule))
        est = pool(k, rule, ev)
        passed = est.value is not None and est.in_range and READINESS_ORDER[est.readiness] >= need
        if passed and est.value is not None:
            circuits[name] = _coerce(k, est.value)
            cited |= {s for e in est.evidence if e.excluded is None for s in e.source_ids}
        if est.readiness == "insufficient" and est.value is None:
            gaps.append(
                Gap(
                    knob=name,
                    kind="no_evidence",
                    sentence=est.sentences[0],
                    ask=f"Add a claim at {path}: {rule.ask}.",
                )
            )
        elif not est.in_range:
            gaps.append(
                Gap(
                    knob=name,
                    kind="out_of_range",
                    sentence=est.sentences[-1],
                    ask=f"Check the claims at {path} and the rule ({rule.formula}).",
                )
            )
        elif not passed:
            gaps.append(
                Gap(
                    knob=name,
                    kind="below_policy",
                    sentence=f"{name} is {est.readiness}; this package passes only "
                    f"{policy.min_readiness} values or better.",
                    ask=f"Verify a claim at {path}, or add one from a second source.",
                )
            )
        if est.agreement == "disagree":
            gaps.append(
                Gap(
                    knob=name,
                    kind="disagree",
                    sentence=f"The claims at {path} disagree.",
                    ask="Review the claims and reject or supersede the one that does not hold.",
                )
            )
        if passed:
            sentence = (
                f"Set to {circuits[name]} {k.unit} ({est.readiness}, strength "
                f"{est.strength:.2f}); circuits' default is {k.default}."
            )
        else:
            sentence = f"Not passed on ({est.readiness}); circuits keeps its default {k.default}."
        entries.append(
            KnobEntry(
                rule=rules.describe(rule),
                estimate=est,
                passed=passed,
                sentence=sentence,
                **common,
            )
        )

    sources = {
        sid: SourceRef(
            doi=s.doi, url=s.url, title=s.title, authors=s.authors, year=s.year, venue=s.venue
        )
        for sid in sorted(cited)
        if (s := store.get("source", sid, Source))
    }
    engineering = sorted(n for n, k in reg.items() if k.kind == "physics")
    n_lit = len(entries)
    n_ready = sum(1 for e in entries if e.estimate and e.estimate.readiness == "ready")
    summary = [
        f"{len(circuits)} of {n_lit} literature knobs get a value from the {clade} evidence "
        f"({n_ready} ready); the other {n_lit - len(circuits)} keep circuits' defaults.",
        f"{len(engineering)} engineering knobs are set by the circuit design, not by papers.",
    ]
    if gaps:
        summary.append(f"{len(gaps)} gap(s) say what to measure or verify next.")
    h = Handoff(
        clade=clade,
        representative_species=reps,
        policy=policy,
        circuits=circuits,
        summary=summary,
        knobs=entries,
        gaps=gaps,
        engineering_knobs=engineering,
        sources=sources,
        provenance=Provenance(
            research_version=__version__,
            research_sha=_research_sha(),
            profiles_sha=git_sha(cfg.profiles_repo) if cfg.profiles_repo else "no checkout",
            circuits_registry_sha=rules.CIRCUITS_SHA,
            quality_version=QUALITY_VERSION,
        ),
    )
    return h.model_copy(update={"digest": h.compute_digest()})


def claim_quality(store: Store, cfg: Settings, claim: Claim) -> dict[str, Any]:
    """A claim's quality as a model input, and the circuit knob it sets (if any)."""
    leaf = claim.library_path.split(".", 1)[1] if "." in claim.library_path else ""
    rule = next((r for r in rules.RULES if r.leaf == leaf), None)
    srcs = [s for sid in claim.source_ids if (s := store.get("source", sid, Source))]
    q = score(claim, srcs, rule=rule, representative=representative_species(cfg, claim.clade))
    return {**q.model_dump(), "sets_knob": rule.knob if rule else None}


def schema() -> dict[str, Any]:
    s = Handoff.model_json_schema()
    s["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    s["title"] = "senseai-research handoff package (to senseai-circuits)"
    return s


def verify(doc: dict[str, Any]) -> Handoff:
    """Parse a package file and check its digest."""
    h = Handoff.model_validate(doc)
    if h.format != FORMAT:
        raise HandoffError(f"unknown package format {h.format!r}")
    if h.digest != h.compute_digest():
        raise HandoffError("the package was changed after it was built (digest mismatch)")
    return h


__all__ = [
    "FORMAT",
    "Gap",
    "Handoff",
    "HandoffError",
    "KnobEntry",
    "Policy",
    "Readiness",
    "build",
    "canonical",
    "clades",
    "claim_quality",
    "representative_species",
    "schema",
    "verify",
]
