# SPDX-License-Identifier: Apache-2.0
"""Chance hit rate: how often an arm would "hit" labelled objects by luck alone.

bench d7c963a reports ``hit_rate`` but no chance rate (docs/SPEC.md, Deviations). This is the
luck check, computed from bench's own output files: each arm's activity timeline
(``timelines/<seq>__<arm>.npz``) is shifted in time, circularly, by ``k / (K + 1)`` of the
recording for ``k = 1 … K``, and re-scored against the same labelled objects
(``replay_report.json`` → ``events``) with the same attribution window and bearing tolerance.
The chance hit rate is the mean over the shifts. Shifting keeps how much and how often an arm
fires, and breaks any link between when it fires and where the objects are.

Detector arms are scored as "active in the window" (the timeline keeps box counts, not boxes),
so their chance rate is an upper bound. If a bench report already carries ``chance_hit_rate``,
that value is used instead.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import numpy as np

SHIFTS = 19


def _hits(
    t_ns: np.ndarray[Any, Any],
    active: np.ndarray[Any, Any],
    bearing: np.ndarray[Any, Any] | None,
    events: Sequence[Mapping[str, Any]],
    pre_ns: int,
    post_ns: int,
    tol_deg: float,
) -> int:
    n = 0
    for ev in events:
        t = int(ev["t_gt_ns"])
        i0 = int(np.searchsorted(t_ns, t - pre_ns, "left"))
        i1 = int(np.searchsorted(t_ns, t + post_ns, "right"))
        a = active[i0:i1]
        if not a.any():
            continue
        if bearing is None:
            n += 1
            continue
        b = bearing[i0:i1][a]
        b = b[np.isfinite(b)]
        if b.size == 0:  # no bearings: "any" attribution, as bench does
            n += 1
            continue
        d = np.abs((b - float(ev["bearing_deg"]) + 180.0) % 360.0 - 180.0)
        if (d <= tol_deg).any():
            n += 1
    return n


def chance_hit_rate(
    report: Mapping[str, Any], timelines: Path, arm: str, shifts: int = SHIFTS
) -> float | None:
    """Mean hit rate of ``arm`` over ``shifts`` circular time shifts; ``None`` if not computable."""
    cfg = report.get("config", {})
    pre_ns = int(float(cfg.get("window_pre_s", 2.0)) * 1e9)
    post_ns = int(float(cfg.get("window_post_s", 1.0)) * 1e9)
    tol = float(cfg.get("bearing_tol_deg", 15.0))
    by_seq: dict[str, list[Mapping[str, Any]]] = {}
    for ev in report.get("events", []):
        by_seq.setdefault(str(ev["sequence"]), []).append(ev)
    total = sum(len(v) for v in by_seq.values())
    if total == 0:
        return None
    kind = report.get("arms", {}).get(arm, {}).get("kind")
    hits = np.zeros(shifts)
    for seq, evs in by_seq.items():
        f = timelines / f"{seq}__{arm}.npz"
        if not f.exists():
            return None
        with np.load(f) as z:
            t_ns = np.asarray(z["t_ns"], np.int64)
            active = np.asarray(z["active"], bool)
            bearing = (
                np.asarray(z["top_bearing_deg"], np.float64)
                if kind == "reflex" and "top_bearing_deg" in z
                else None
            )
        if t_ns.size < 2:
            return None
        for k in range(shifts):
            s = round(t_ns.size * (k + 1) / (shifts + 1))
            hits[k] += _hits(
                t_ns,
                np.roll(active, s),
                None if bearing is None else np.roll(bearing, s),
                evs,
                pre_ns,
                post_ns,
                tol,
            )
    return float(hits.mean() / total)


__all__ = ["SHIFTS", "chance_hit_rate"]
