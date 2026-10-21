# SPDX-License-Identifier: Apache-2.0
"""Re-derive every profile against a patched library and diff the derived values (spec §5.2).

Both sides go through the same code the robot's profiles go through: ``distill_spec`` (rules
on library values) then ``registry.compose`` (with the sensor specs). Nothing is estimated.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

import numpy as np

from senseai_research.impact.consumers import ProfilesTree, load
from senseai_research.ledger.models import FieldSummary, ProfileRow, ReflexRow

#: bench arm -> profile, copied from senseai_bench.replay.cli.SDK_PROFILES (bench d7c963a).
ARM_PROFILES: Mapping[str, str] = {
    "panther_prior": "panther_vision",
    "panther_vision": "panther_vision",
    "panther_fused": "panther_vision",
    "panther": "panther",
    "null_profile": "null",
}
#: Reflex decision D1: the reflex threshold scales with the commit fraction (0.5 -> 2.6).
THETA_AT_HALF = 2.6


def theta(commit_threshold: float) -> float:
    return THETA_AT_HALF * commit_threshold / 0.5


Flat = dict[str, float | str]  # point label -> value


def _num(v: Any) -> float | str:
    if isinstance(v, (int, float, np.floating, np.integer)):
        return float(v)
    return str(v)


def flatten(value: Any) -> tuple[str, Flat]:
    """A composed prior value as ``(kind, {point: number})``."""
    from senseai_profiles.rules import SceneTable, Static

    if isinstance(value, SceneTable):
        xs, ys = np.asarray(value.x), np.asarray(value.y)
        return "table", {f"at {x:.3g} lux": float(y) for x, y in zip(xs, ys, strict=True)}
    v = value.value if isinstance(value, Static) else value
    if isinstance(v, tuple) and len(v) == 2 and all(isinstance(a, (tuple, list)) for a in v):
        return "table", {f"at {x:.3g} lux": float(y) for x, y in zip(v[0], v[1], strict=True)}
    if isinstance(v, Mapping):
        return "weights", {str(k): _num(x) for k, x in sorted(v.items())}
    if isinstance(v, np.ndarray):
        flat = v.ravel()
        return "array", {f"[{i}]": float(x) for i, x in enumerate(flat)}
    return "scalar", {"": _num(v)}


def derive(tree: ProfilesTree, library_dir: Path) -> dict[str, dict[str, Any]]:
    """Every profile's composed values: ``{profile: {"heads": {(sec, field): (rule, value)},
    "commit_threshold": float}}``."""
    from senseai_profiles.distill import distill_spec
    from senseai_profiles.registry import compose
    from senseai_profiles.sensors import load_sensors
    from senseai_profiles.spec import RuleCall

    lib, specs = load(tree, library_dir)
    sensors = load_sensors(tree.sensors) if tree.sensors.exists() else {}
    out: dict[str, dict[str, Any]] = {}
    for name, spec in sorted(specs.items()):
        bundle = distill_spec(spec, lib)
        prof = compose(bundle, spec, sensors)
        heads: dict[tuple[str, str], tuple[str, Any]] = {}
        for head, fields in prof.heads.items():
            for fld, val in fields.items():
                raw = spec.heads.get(head, {}).get(fld)
                rule = raw.name if isinstance(raw, RuleCall) else "literal"
                heads[(head, fld)] = (rule, val)
        out[name] = {"heads": heads, "commit_threshold": float(prof.reflex.commit_threshold)}
    return out


def _delta_pct(a: float | str | None, b: float | str | None) -> float | None:
    if not isinstance(a, float) or not isinstance(b, float):
        return None
    if a == 0:
        return None if b == 0 else math.copysign(math.inf, b)
    return (b - a) / abs(a) * 100.0


def _lux(label: str) -> float:
    return float(label.removeprefix("at ").removesuffix(" lux"))


def _crossing(points: Flat, level: float) -> float | None:
    """Lux at which a monotone table crosses ``level`` (log-linear interpolation)."""
    xs = [_lux(k) for k in points]
    ys = [float(v) for v in points.values()]
    for i in range(1, len(xs)):
        y0, y1 = ys[i - 1], ys[i]
        if (y0 - level) * (y1 - level) <= 0 and y0 != y1:
            f = (level - y0) / (y1 - y0)
            return float(
                10 ** (math.log10(xs[i - 1]) + f * (math.log10(xs[i]) - math.log10(xs[i - 1])))
            )
    return None


def _fmt_lux(x: float | None) -> str:
    return "nowhere on the 0.001 to 100,000 lux range" if x is None else f"{x:.3g} lux"


def _sentence(profile: str, section: str, fld: str, old: Flat, new: Flat) -> str:
    if fld == "gain_lut":
        a, b = _crossing(old, 0.5), _crossing(new, 0.5)
        return (
            f"{profile}: the frame head trusts the camera image at half confidence down to "
            f"{_fmt_lux(b)} (was {_fmt_lux(a)}). Below that, Panther leans less on the frame."
        )
    if fld == "attention" and section == "event":

        def start(t: Flat) -> float | None:
            boosted = [_lux(k) for k, v in t.items() if float(v) > 1.001]
            return max(boosted) if boosted else None

        def peak(t: Flat) -> float:
            return max((float(v) for v in t.values()), default=1.0)

        return (
            f"{profile}: the event camera's low-light boost starts below {_fmt_lux(start(new))} "
            f"(was {_fmt_lux(start(old))}) and peaks at x{peak(new):.2f} (was x{peak(old):.2f})."
        )
    return f"{profile}: {section}.{fld} changes."


def diff(
    before: Mapping[str, Mapping[str, Any]], after: Mapping[str, Mapping[str, Any]]
) -> tuple[list[ProfileRow], list[FieldSummary], int]:
    """Rows for every changed point, one summary per changed field, and the unchanged count."""
    rows: list[ProfileRow] = []
    sums: list[FieldSummary] = []
    unchanged = 0
    for prof in sorted(set(before) | set(after)):
        b_heads = before.get(prof, {}).get("heads", {})
        a_heads = after.get(prof, {}).get("heads", {})
        for key in sorted(set(b_heads) | set(a_heads)):
            section, fld = key
            rule_b, val_b = b_heads.get(key, ("absent", None))
            rule_a, val_a = a_heads.get(key, ("absent", None))
            _, fb = flatten(val_b) if val_b is not None else ("absent", {})
            _, fa = flatten(val_a) if val_a is not None else ("absent", {})
            changed: list[ProfileRow] = []
            for pt in list(dict.fromkeys([*fb, *fa])):
                ob, na = fb.get(pt), fa.get(pt)
                same = (
                    isinstance(ob, float)
                    and isinstance(na, float)
                    and math.isclose(ob, na, rel_tol=1e-12, abs_tol=1e-12)
                ) or ob == na
                if same:
                    continue
                changed.append(
                    ProfileRow(
                        profile=prof,
                        section=section,
                        field=fld,
                        point=pt,
                        old=ob,
                        new=na,
                        delta_pct=_delta_pct(ob, na),
                    )
                )
            if not changed:
                unchanged += 1
                continue
            rows.extend(changed)
            finite = [
                abs(r.delta_pct)
                for r in changed
                if r.delta_pct is not None and math.isfinite(r.delta_pct)
            ]
            sums.append(
                FieldSummary(
                    profile=prof,
                    section=section,
                    field=fld,
                    rule=rule_a if rule_a != "absent" else rule_b,
                    changed_points=len(changed),
                    max_abs_delta_pct=max(finite) if finite else None,
                    sentence=_sentence(prof, section, fld, fb, fa),
                )
            )
    return rows, sums, unchanged


def reflex_rows(
    before: Mapping[str, Mapping[str, Any]],
    after: Mapping[str, Mapping[str, Any]],
    arms: Iterable[str] = ARM_PROFILES,
) -> list[ReflexRow]:
    out = []
    for arm in arms:
        prof = ARM_PROFILES.get(arm)
        if prof is None or prof not in before or prof not in after:
            continue
        ct_b = float(before[prof]["commit_threshold"])
        ct_a = float(after[prof]["commit_threshold"])
        out.append(
            ReflexRow(
                arm=arm,
                profile=prof,
                commit_threshold_old=ct_b,
                commit_threshold_new=ct_a,
                theta_old=theta(ct_b),
                theta_new=theta(ct_a),
            )
        )
    return out


__all__ = ["ARM_PROFILES", "THETA_AT_HALF", "derive", "diff", "flatten", "reflex_rows", "theta"]
