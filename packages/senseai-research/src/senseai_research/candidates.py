# SPDX-License-Identifier: Apache-2.0
"""Circuit candidates: library values scored for whether they can become part of a circuit prior.

senseai-circuits holds the fixed, weight-free circuits that learned models train on top of.
Their knobs need numbers from the library, and someone has to judge which numbers can be
trusted, which apply to the target species, and which a circuit can use at all. This
module produces one **candidate** per library value (and per value from another clade that
would set a circuit knob), with two scores computed by code from the ledger:

``quality`` (is the number true?)
    claim status × library confidence × method × how much of the source a person read.

``likelihood`` (does it apply to the target species?)
    species distance between where it was measured and the target clade's representative
    species, times agreement (a disputed claim on the same path lowers it).

Every factor is in ``score_parts`` with its weight, so a person can see why a value ranks
where it does. The weights below are proposed, not fitted.

senseai-circuits then adds what only it can say: **circuitability**, whether the value sets
an existing knob, fits a circuit type without a knob for it, needs a new circuit, or is not
circuit material, and ``priority = likelihood × quality × circuitability score``. This module
runs ``senseai-circuits assess`` (``SENSEAI_CIRCUITS_CMD``) on the candidates in the
``senseai-circuits/candidate@1`` format; without it, circuitability is "not checked".

People design circuits from candidates in senseai-studio (Design tab), which reports back
through :func:`decide` (in design, adopted, rejected). Candidates never change the library.
"""

from __future__ import annotations

import json
import shlex
import shutil
import subprocess
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

import yaml

from senseai_research.config import Settings
from senseai_research.ledger.models import CircuitCandidate, Claim, Passage, Source
from senseai_research.ledger.store import LedgerError, Store

ACTOR = "pipeline:candidates@0.1.0"
CIRCUITS_SCHEMA = "senseai-circuits/candidate@1"

#: quality factors (proposed weights)
STATUS_WEIGHT = {
    "verified": 1.0,
    "extracted": 0.75,
    "disputed": 0.35,
    "unverified": 0.25,
    "rejected": 0.0,
    "superseded": 0.0,
}
CONFIDENCE_WEIGHT = {"high": 1.0, "medium": 0.7, "low": 0.4}
METHOD_WEIGHT = {
    "electrophysiology": 1.0,
    "behavioural": 0.9,
    "anatomical": 0.8,
    "review": 0.6,
    "model": 0.5,
}
#: the CITATIONS.md "Checked" column, best first: how much of the paper a person read
READ_WEIGHT = (("full", 1.0), ("abstract", 0.7), ("secondary", 0.5), ("record", 0.4))
#: likelihood factors (proposed weights)
SPECIES_WEIGHT = {
    "same species": 1.0,
    "same genus": 0.9,
    "same clade": 0.75,
    "species not recorded": 0.6,
    "other clade": 0.35,
}
DISPUTED_FACTOR = 0.7
DECISIONS = ("open", "in_design", "adopted", "rejected")


# --------------------------------------------------------------------------- library


def _library(cfg: Settings) -> dict[str, dict[str, Any]]:
    """``{clade key: entry}`` from ``<profiles>/library/*.yaml``."""
    root = (cfg.profiles_repo or cfg.stack / "profiles") / "library"
    out: dict[str, dict[str, Any]] = {}
    for f in sorted(root.glob("*.yaml")):
        try:
            doc = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError:
            continue
        if isinstance(doc, dict) and doc.get("clade"):
            out[f.stem] = doc
    return out


def _senses(entry: Mapping[str, Any]) -> dict[str, str | None]:
    return {
        str(s.get("id")): s.get("modality")
        for s in entry.get("senses") or []
        if isinstance(s, dict)
    }


def _representatives(entry: Mapping[str, Any]) -> list[str]:
    return [
        str(r.get("name")) for r in entry.get("representative_species") or [] if isinstance(r, dict)
    ]


def _sense_of(path: str) -> str | None:
    parts = path.split(".")
    return parts[2] if len(parts) > 3 and parts[1] == "senses" else None


# --------------------------------------------------------------------------- scores


def quality(store: Store, c: Claim) -> tuple[float, dict[str, Any]]:
    """How far the number can be trusted, from the ledger alone."""
    status = STATUS_WEIGHT[c.status]
    conf = CONFIDENCE_WEIGHT.get(c.library_confidence or "", 0.6)
    method = METHOD_WEIGHT.get(c.method or "", 0.7)
    read, read_label = 0.5, "not recorded"
    for sid in c.source_ids:
        s = store.get("source", sid, Source)
        checked = (s.checked or "").lower() if s else ""
        for word, w in READ_WEIGHT:
            if word in checked and (read_label == "not recorded" or w > read):
                read, read_label = w, s.checked if s and s.checked else word
                break
    q = status * conf * method * read
    return round(q, 4), {
        "status": {"value": c.status, "weight": status},
        "confidence": {"value": c.library_confidence, "weight": conf},
        "method": {"value": c.method, "weight": method},
        "read": {"value": read_label, "weight": read},
        "passage": bool(c.passage_id),
    }


def species_relation(
    species: str | None, evidence_clade: str, target_clade: str, reps: Iterable[str]
) -> str:
    if not species:
        return "species not recorded"
    reps = list(reps)
    if species in reps:
        return "same species"
    if any(species.split()[0] == r.split()[0] for r in reps):
        return "same genus"
    return "same clade" if evidence_clade == target_clade else "other clade"


def likelihood(
    store: Store, c: Claim, target_clade: str, reps: Iterable[str]
) -> tuple[float, dict[str, Any]]:
    """How likely the measurement holds for the target species."""
    rel = species_relation(c.species, c.clade, target_clade, reps)
    sp = SPECIES_WEIGHT[rel]
    disputed = (
        any(
            o.library_path == c.library_path and o.status == "disputed" and o.id != c.id
            for o in store.claims()
        )
        or c.status == "disputed"
    )
    agree = DISPUTED_FACTOR if disputed else 1.0
    return round(sp * agree, 4), {
        "species": {"value": rel, "measured_in": c.species, "target": list(reps), "weight": sp},
        "agreement": {"value": "disputed" if disputed else "no dispute", "weight": agree},
    }


# --------------------------------------------------------------------------- circuits


def circuits_command(cfg: Settings) -> list[str] | None:
    parts = shlex.split(cfg.circuits_cmd)
    if not parts or not (shutil.which(parts[0]) or Path(parts[0]).exists()):
        return None
    return parts


def assess(cfg: Settings, payload: list[dict[str, Any]]) -> tuple[list[dict[str, Any]] | None, str]:
    """``senseai-circuits assess -`` on candidate@1 records: (assessments, note)."""
    cmd = circuits_command(cfg)
    if cmd is None:
        return None, f"senseai-circuits not found ({cfg.circuits_cmd}): circuitability not checked"
    if not payload:
        return [], ""
    try:
        cp = subprocess.run(
            [*cmd, "assess", "-"],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        out = json.loads(cp.stdout)
    except (OSError, subprocess.TimeoutExpired, ValueError) as e:
        return None, f"senseai-circuits assess failed: {type(e).__name__}: {e}"
    if cp.returncode != 0 or not isinstance(out, list):
        err = out.get("error") if isinstance(out, dict) else cp.stderr.strip()[-300:]
        return None, f"senseai-circuits assess failed: {err}"
    return out, ""


def circuits_version(cfg: Settings) -> str | None:
    cmd = circuits_command(cfg)
    if cmd is None:
        return None
    try:
        cp = subprocess.run(
            [*cmd, "catalogue", "--json"], capture_output=True, text=True, timeout=60, check=False
        )
        v = json.loads(cp.stdout).get("circuits_version")
    except (OSError, subprocess.TimeoutExpired, ValueError, AttributeError):
        return None
    return str(v) if v else None


# --------------------------------------------------------------------------- records


def candidate_id(target_clade: str, sense: str, library_path: str) -> str:
    return f"cc:{target_clade}.{sense}:{library_path}"


def evidence(store: Store, c: Claim) -> dict[str, Any]:
    q = c.quantity
    quote = None
    if c.passage_id and (p := store.get("passage", c.passage_id, Passage)):
        quote = p.text
    return {
        "claim_id": c.id,
        "library_path": c.library_path,
        "value": q.value,
        "low": q.low,
        "high": q.high,
        "unit": q.unit,
        "status": c.status,
        "species": c.species,
        "method": c.method,
        "conditions": c.conditions,
        "passage": bool(c.passage_id),
        "quote": quote,
    }


def to_circuits(store: Store, cand: CircuitCandidate) -> dict[str, Any]:
    """The candidate in senseai-circuits' ``candidate@1`` format."""
    claims = [store.must("claim", cid, Claim) for cid in cand.claim_ids]
    return {
        "schema": CIRCUITS_SCHEMA,
        "id": cand.id,
        "clade": cand.clade,
        "sense": cand.sense,
        "modality": cand.modality,
        "mechanism": cand.mechanism,
        "evidence": [evidence(store, c) for c in claims],
        "proposed": cand.proposed,
        "scores": {
            "likelihood": cand.likelihood,
            "quality": cand.quality,
            "parts": cand.score_parts,
        },
    }


def _draft(
    store: Store, c: Claim, target: str, sense: str, modality: str | None, reps: list[str]
) -> CircuitCandidate:
    lq, lp = likelihood(store, c, target, reps)
    qq, qp = quality(store, c)
    where = "" if c.clade == target else f" (measured in {c.clade})"
    return CircuitCandidate(
        id=candidate_id(target, sense, c.library_path),
        created_by=ACTOR,
        clade=target,
        sense=sense,
        modality=modality,
        mechanism=f"{c.library_path} = {c.quantity.text()}{where}",
        claim_ids=[c.id],
        library_path=c.library_path,
        evidence_clade=c.clade,
        likelihood=lq,
        quality=qq,
        score_parts={"likelihood": lp, "quality": qp},
    )


def derive(store: Store, cfg: Settings, *, actor: str = ACTOR) -> dict[str, Any]:
    """Score every claim as a candidate for its own clade's sense, and every claim that would
    set a circuit knob as a candidate for the other clades' senses of the same modality.
    Stores candidates whose circuitability is not ``none`` (or all, when circuits is missing).
    """
    lib = _library(cfg)
    own: list[CircuitCandidate] = []
    cross: list[CircuitCandidate] = []
    claims = [c for c in store.claims() if c.status not in ("rejected", "superseded")]
    for c in claims:
        sense = _sense_of(c.library_path)
        entry = lib.get(c.clade)
        if sense is None or entry is None:
            continue
        modality = _senses(entry).get(sense)
        own.append(_draft(store, c, c.clade, sense, modality, _representatives(entry)))
        for key, other in lib.items():
            if key == c.clade:
                continue
            for s_id, mod in _senses(other).items():
                if modality and mod == modality:
                    cross.append(_draft(store, c, key, s_id, mod, _representatives(other)))
    drafts = own + cross
    results, note = assess(cfg, [to_circuits(store, d) for d in drafts])
    version = circuits_version(cfg) if results is not None else None
    stored = skipped = 0
    for i, d in enumerate(drafts):
        a = results[i] if results is not None else None
        is_cross = i >= len(own)
        if a is not None and (a.get("level") == "none" or (is_cross and a.get("level") != "knob")):
            skipped += 1
            continue
        if results is None and is_cross:
            continue  # cross-clade candidates need circuits to say they set a knob
        d.circuitability = a
        d.priority = float(a["priority"]) if a and a.get("priority") is not None else None
        d.circuits_version = version
        d.circuits_note = note
        prev = store.get("candidate", d.id, CircuitCandidate)
        if prev is not None:  # keep the people's side: status, designs, decisions
            d.status, d.design_ids = prev.status, prev.design_ids
            d.decided_by, d.decision_rationale = prev.decided_by, prev.decision_rationale
            d.created_at, d.created_by = prev.created_at, prev.created_by
        store.put_candidate(d, actor)
        stored += 1
    return {
        "stored": stored,
        "skipped": skipped,
        "circuits": note or "checked",
        "circuits_version": version,
    }


def ranked(
    store: Store, clade: str | None = None, sense: str | None = None
) -> list[CircuitCandidate]:
    """Candidates, highest priority first (unchecked ones by likelihood × quality, after)."""
    out = [
        c
        for c in store.all("candidate", CircuitCandidate)
        if (clade is None or c.clade == clade) and (sense is None or c.sense == sense)
    ]
    return sorted(
        out,
        key=lambda c: (c.priority is None, -(c.priority or c.likelihood * c.quality), c.id),
    )


def decide(
    store: Store,
    cid: str,
    status: str,
    *,
    by: str,
    rationale: str,
    design_id: str | None = None,
) -> CircuitCandidate:
    """A person moves a candidate (Studio's Design tab): in design, adopted, rejected, open."""
    if status not in DECISIONS:
        raise LedgerError(f"status must be one of {', '.join(DECISIONS)}")
    if not by.startswith("user:"):
        raise LedgerError("only a person decides on a candidate")
    if len(rationale.strip().split()) < 3:
        raise LedgerError("rationale must be at least one sentence")
    c = store.must("candidate", cid, CircuitCandidate)
    c.status = status  # type: ignore[assignment]
    if design_id and design_id not in c.design_ids:
        c.design_ids = [*c.design_ids, design_id]
    c.decided_by, c.decision_rationale = by, rationale.strip()
    store.put_candidate(c, by)
    return c


__all__ = [
    "CIRCUITS_SCHEMA",
    "DECISIONS",
    "assess",
    "candidate_id",
    "decide",
    "derive",
    "likelihood",
    "quality",
    "ranked",
    "to_circuits",
]
