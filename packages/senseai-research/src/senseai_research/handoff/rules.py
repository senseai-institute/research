# SPDX-License-Identifier: Apache-2.0
"""Which library measurement sets which circuit knob, and how (docs/HANDOFF.md §Rules).

The knobs come from senseai-circuits' registry. Research does not import circuits (it would
pull its NumPy stack into this service), so ``circuits_knobs.json`` is a copy of
``senseai-circuits knobs --json`` at the commit in :data:`CIRCUITS_SHA`. A test compares the
copy with the installed package when there is one.

A rule names a library leaf relative to the clade (``senses.hearing.spatial_resolution``), the
unit the leaf must be in once normalised (``curate.units``), and a transform from that value to
the knob's unit. Every transform is written out in ``formula`` so a reviewer can check it.

Leaves that no library entry has yet (``senses.vision.rgc_center_radius`` and the like) are
asks: the gaps list tells a contributor which measurement would set which knob. Their names
follow the circuits design table and may be renamed when profiles' P0 species-file document
lands (circuits docs/DESIGN.md, deviation 4).
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache
from importlib import resources
from typing import Any, Literal

#: senseai-circuits commit the registry copy was taken from.
CIRCUITS_SHA = "f465ce15033faba96b08acd4d264d42ae0a51c1c"

KnobKind = Literal["physics", "measured", "proposed"]


@dataclass(frozen=True)
class Knob:
    name: str
    unit: str
    default: float | int | bool
    low: float
    high: float
    kind: KnobKind
    type: str  # float | int | bool
    source: str
    doc: str


@lru_cache(maxsize=1)
def registry_text() -> str:
    return resources.files(__package__).joinpath("circuits_knobs.json").read_text()


@lru_cache(maxsize=1)
def knobs() -> dict[str, Knob]:
    out: dict[str, Knob] = {}
    for k in json.loads(registry_text()):
        lo, hi = k["range"]
        out[k["name"]] = Knob(
            name=k["name"],
            unit=k["unit"],
            default=k["default"],
            low=float(lo),
            high=float(hi),
            kind=k["kind"],
            type=k["type"],
            source=k["source"],
            doc=k["doc"],
        )
    return out


@dataclass(frozen=True)
class Rule:
    knob: str
    leaf: str  # library path below the clade
    unit: str  # the leaf's normalised unit (curate.units)
    formula: str  # the transform, in words a reviewer can check
    transform: Callable[[float], float]
    mapping: Literal["exact", "close", "loose"]
    methods: tuple[str, ...]  # methods that measure this quantity directly
    ask: str  # what to measure, said to a contributor
    why: str  # why this leaf sets this knob

    def apply(self, x: float) -> float:
        return float(self.transform(x))


def _same(x: float) -> float:
    return x


RULES: tuple[Rule, ...] = (
    Rule(
        knob="ts.sigma_bearing_deg",
        leaf="senses.hearing.spatial_resolution",
        unit="deg",
        formula="σ_bearing = minimum audible angle",
        transform=_same,
        mapping="close",
        methods=("behavioural",),
        ask="the minimum audible angle (sound localisation acuity) in degrees, behavioural",
        why=(
            "the token matcher's bearing tolerance is the smallest angle the animal can tell "
            "apart; senseai-world's prior reads the same leaf"
        ),
    ),
    Rule(
        knob="cs.sigma_center_deg",
        leaf="senses.vision.rgc_center_radius",
        unit="deg",
        formula="σ_center = r_center / √2",
        transform=lambda r: r / math.sqrt(2.0),
        mapping="exact",
        methods=("electrophysiology",),
        ask=(
            "the receptive-field centre radius of retinal ganglion cells near the area "
            "centralis, in degrees, from a difference-of-Gaussians fit"
        ),
        why="the retina stage is a difference of Gaussians; its centre width is this radius",
    ),
    Rule(
        knob="cs.surround_ratio",
        leaf="senses.vision.rgc_surround_ratio",
        unit="ratio",
        formula="surround_ratio = r_surround / r_center",
        transform=_same,
        mapping="exact",
        methods=("electrophysiology",),
        ask="the ratio of surround to centre radius of retinal ganglion cell receptive fields",
        why="the retina stage's surround Gaussian is this many times wider than the centre",
    ),
    Rule(
        knob="cs.adapt_tau_ms",
        leaf="senses.vision.light_adaptation_tau",
        unit="ms",
        formula="τ_adapt = time constant of light adaptation",
        transform=_same,
        mapping="close",
        methods=("electrophysiology", "behavioural"),
        ask="the time constant of retinal light adaptation (gain change after a step), in ms",
        why="the retina divides each channel by a running mean with this time constant",
    ),
    Rule(
        knob="emd.delay_tau_ms",
        leaf="senses.vision.motion_tf_optimum",
        unit="Hz",
        formula="τ_delay = 1000 / (2π · f_opt) ms (first-order low-pass Reichardt detector)",
        transform=lambda f: 1000.0 / (2.0 * math.pi * f),
        mapping="close",
        methods=("electrophysiology", "behavioural"),
        ask=(
            "the temporal-frequency optimum of direction-selective motion responses, in Hz "
            "(the stimulus-independent peak of a Reichardt-type detector)"
        ),
        why=(
            "a Reichardt detector with a first-order low-pass delay τ responds most at "
            "f = 1/(2πτ) (Borst & Egelhaaf 1989)"
        ),
    ),
    Rule(
        knob="lgmd.ttc_alpha",
        leaf="senses.vision.looming_peak_angle",
        unit="deg",
        formula="α = 2 / θ_peak",
        transform=lambda theta: 2.0 / theta,
        mapping="close",
        methods=("electrophysiology", "behavioural"),
        ask=("the angular size at which looming-sensitive responses (or escape) peak, in degrees"),
        why="η(t) = C·θ̇·exp(−αθ) peaks at θ = 2/α (Gabbiani, Krapp & Laurent 1999)",
    ),
)

#: Knobs of kind ``measured`` or ``proposed`` that no published quantity sets directly, and why.
NO_LITERATURE: dict[str, str] = {
    "lgmd.inhib_spread_px": "a model width in grid cells; it depends on the sensor's sampling",
    "lgmd.w_inhib": "a fitted model weight; set it from experiments, not from a paper",
    "lgmd.ffi_threshold": "a model threshold; set it from experiments, not from a paper",
    "x.w_loom": "a behavioural weight; set it from experiments, not from a paper",
    "ts.integration_ticks": (
        "counted in sensor ticks, so it needs the tick length as well as the animal's "
        "temporal integration (a library open question)"
    ),
    "ts.w_against": (
        "a behavioural preference (felidae.behaviours.pounce is qualitative); set it from "
        "experiments"
    ),
}


def rules_by_knob() -> dict[str, Rule]:
    return {r.knob: r for r in RULES}


def check() -> list[str]:
    """Problems with the rule table against the registry copy (a test asserts none)."""
    reg = knobs()
    out = []
    for r in RULES:
        if r.knob not in reg:
            out.append(f"rule for unknown knob {r.knob}")
        elif reg[r.knob].kind == "physics":
            out.append(f"{r.knob} is a physics knob; literature does not set it")
    for n in NO_LITERATURE:
        if n not in reg:
            out.append(f"NO_LITERATURE names unknown knob {n}")
    covered = {r.knob for r in RULES} | set(NO_LITERATURE)
    for k in reg.values():
        if k.kind != "physics" and k.name not in covered:
            out.append(f"{k.name} ({k.kind}) has neither a rule nor a reason")
    return out


def describe(r: Rule) -> dict[str, Any]:
    return {
        "knob": r.knob,
        "leaf": r.leaf,
        "unit": r.unit,
        "formula": r.formula,
        "mapping": r.mapping,
        "methods": list(r.methods),
        "why": r.why,
    }


__all__ = [
    "CIRCUITS_SHA",
    "NO_LITERATURE",
    "RULES",
    "Knob",
    "Rule",
    "check",
    "describe",
    "knobs",
    "registry_text",
    "rules_by_knob",
]
