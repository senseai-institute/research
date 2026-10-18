# SPDX-License-Identifier: Apache-2.0
"""Experiment records and the baseline-vs-patched headline (N5).

Every number shown for an experiment comes from :func:`outputs_from_report` (bench's
``replay_report.json`` plus :mod:`chance` on bench's timelines) and is stored in the
:class:`Experiment` record. The headline reads only stored records.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from senseai_research.experiments.chance import chance_hit_rate
from senseai_research.ledger.models import ArmResult, Experiment

#: Fewer labelled objects than this and an arm's hit rate is not judged (viewer MIN_OBJECTS).
MIN_OBJECTS = 20


def _f(v: Any) -> float | None:
    if v is None or isinstance(v, str):
        return None
    x = float(v)
    return x if math.isfinite(x) else None


def outputs_from_report(report: Mapping[str, Any], report_dir: Path) -> dict[str, ArmResult]:
    out: dict[str, ArmResult] = {}
    for arm, a in report.get("arms", {}).items():
        chance = a.get("chance_hit_rate")
        if chance is None:
            chance = chance_hit_rate(report, report_dir / "timelines", arm)
        out[arm] = ArmResult(
            arm=arm,
            tracks=a.get("tracks"),
            hits=a.get("hits"),
            hit_rate=_f(a.get("hit_rate")),
            chance_hit_rate=_f(chance),
            median_delay_s=_f((a.get("delay") or {}).get("median_s")),
            unattributed_fires_per_h=_f(a.get("unattributed_fires_per_hour")),
            latency_p99_ms=_f((a.get("latency") or {}).get("p99_ms")),
            errors=list(a.get("errors") or []),
        )
    return out


def cache_key(
    sequences: Iterable[str], arms: Iterable[str], shas: Mapping[str, str], extra: Mapping[str, Any]
) -> str:
    doc = {"sequences": sorted(sequences), "arms": sorted(arms), "shas": dict(shas), **extra}
    return hashlib.sha256(json.dumps(doc, sort_keys=True).encode()).hexdigest()


class ComparisonRow(BaseModel):
    """One arm, baseline next to patched. Numbers are copies of stored record fields."""

    model_config = ConfigDict(extra="forbid")

    arm: str
    objects: int | None
    judged: bool
    baseline_hit_rate: float | None
    baseline_chance_hit_rate: float | None
    patched_hit_rate: float | None
    patched_chance_hit_rate: float | None
    baseline_median_delay_s: float | None
    patched_median_delay_s: float | None
    baseline_fires_per_h: float | None
    patched_fires_per_h: float | None
    baseline_latency_p99_ms: float | None
    patched_latency_p99_ms: float | None
    verdict: str


def _pct(v: float | None) -> str:
    return "n/a" if v is None else f"{100 * v:.0f}%"


def compare(baseline: Experiment | None, patched: Experiment) -> list[ComparisonRow]:
    rows = []
    for arm in patched.arms:
        p = patched.outputs.get(arm, ArmResult(arm=arm))
        b = baseline.outputs.get(arm, ArmResult(arm=arm)) if baseline else ArmResult(arm=arm)
        objects = p.tracks if p.tracks is not None else b.tracks
        judged = objects is not None and objects >= MIN_OBJECTS
        if p.errors and p.hit_rate is None:
            verdict = f"did not run: {p.errors[0]}"
        elif not judged:
            verdict = f"too few objects to judge ({objects or 0} labelled, need {MIN_OBJECTS})"
        else:
            verdict = (
                f"hit rate {_pct(b.hit_rate)} → {_pct(p.hit_rate)} "
                f"(luck alone: {_pct(b.chance_hit_rate)} → {_pct(p.chance_hit_rate)})"
            )
        rows.append(
            ComparisonRow(
                arm=arm,
                objects=objects,
                judged=judged,
                baseline_hit_rate=b.hit_rate if judged else None,
                baseline_chance_hit_rate=b.chance_hit_rate if judged else None,
                patched_hit_rate=p.hit_rate if judged else None,
                patched_chance_hit_rate=p.chance_hit_rate if judged else None,
                baseline_median_delay_s=b.median_delay_s,
                patched_median_delay_s=p.median_delay_s,
                baseline_fires_per_h=b.unattributed_fires_per_h,
                patched_fires_per_h=p.unattributed_fires_per_h,
                baseline_latency_p99_ms=b.latency_p99_ms,
                patched_latency_p99_ms=p.latency_p99_ms,
                verdict=verdict,
            )
        )
    return rows


def table(rows: list[ComparisonRow]) -> str:
    """The comparison as a Markdown table (PR body, CLI)."""

    def n(v: float | None, fmt: str) -> str:
        return "—" if v is None else format(v, fmt)

    out = [
        "| arm | objects | hit rate (baseline → patched) | chance (baseline → patched) "
        "| median delay s | false fires/h | p99 ms |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        hit = (
            f"{_pct(r.baseline_hit_rate)} → {_pct(r.patched_hit_rate)}"
            if r.judged
            else "too few objects to judge"
        )
        ch = (
            f"{_pct(r.baseline_chance_hit_rate)} → {_pct(r.patched_chance_hit_rate)}"
            if r.judged
            else "—"
        )
        out.append(
            f"| {r.arm} | {r.objects if r.objects is not None else '—'} | {hit} | {ch} "
            f"| {n(r.baseline_median_delay_s, '.2f')} → {n(r.patched_median_delay_s, '.2f')} "
            f"| {n(r.baseline_fires_per_h, '.1f')} → {n(r.patched_fires_per_h, '.1f')} "
            f"| {n(r.baseline_latency_p99_ms, '.1f')} → {n(r.patched_latency_p99_ms, '.1f')} |"
        )
    return "\n".join(out)


__all__ = ["MIN_OBJECTS", "ComparisonRow", "cache_key", "compare", "outputs_from_report", "table"]
