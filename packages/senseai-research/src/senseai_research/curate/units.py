# SPDX-License-Identifier: Apache-2.0
"""Unit normalisation for claims: pint for physical units, a literal table for the rest."""

from __future__ import annotations

from functools import lru_cache

import pint

# Units that are not physical quantities (or that pint should not touch): spelled as the
# library spells them. Keys are lower-case variants seen in papers.
LITERAL_UNITS: dict[str, str] = {
    "log10 re human": "log10 re human",
    "log units re human": "log10 re human",
    "log unit re human": "log10 re human",
    "log units": "log10 re human",
    "ratio": "ratio",
    "fold": "ratio",
    "x": "ratio",
    "count": "count",
    "n": "count",
    "photons at cornea": "photons at cornea",
    "quanta at the cornea": "photons at cornea",
    "cycles/deg": "cycles/deg",
    "cycles per degree": "cycles/deg",
    "cycles/degree": "cycles/deg",
    "c/deg": "cycles/deg",
    "cpd": "cycles/deg",
    "cells/mm^2": "cells/mm^2",
    "cells/mm2": "cells/mm^2",
    "/mm2": "cells/mm^2",
    "per mm2": "cells/mm^2",
    "rods/mm2": "cells/mm^2",
    "cones/mm2": "cells/mm^2",
    "db spl": "dB SPL",
    "db": "dB",
    "events/s/px": "events/s/px",
}

# Canonical spelling per pint dimensionality we care about.
CANONICAL: dict[str, str] = {
    "[time]": "ms",
    "1 / [time]": "Hz",
    "[length]": "m",
    "[luminous_flux] / [length] ** 2": "lux",
    "[luminosity] / [length] ** 2": "cd/m^2",
    "": "deg",  # dimensionless angle handled separately
}


@lru_cache(maxsize=1)
def registry() -> pint.UnitRegistry:
    ureg = pint.UnitRegistry()
    ureg.define("cycle = [cycle]")
    return ureg


def normalise_unit(unit: str) -> tuple[str, float] | None:
    """Return ``(canonical_unit, factor)`` so that ``value * factor`` is in the canonical unit.

    ``None`` when the unit is unknown to both tables (the claim is kept and flagged).
    """
    u = unit.strip()
    key = u.lower()
    if key in LITERAL_UNITS:
        return LITERAL_UNITS[key], 1.0
    ureg = registry()
    try:
        q = ureg.Quantity(1.0, u.replace("^", "**"))
    except Exception:
        return None
    dim = str(q.dimensionality)
    if dim == "dimensionless":
        if key in ("deg", "degree", "degrees", "°"):
            return "deg", 1.0
        if key in ("arcmin", "min of arc", "minutes of arc", "'"):
            return "deg", 1.0 / 60.0
        if key in ("rad", "radian"):
            return "deg", 57.29577951308232
        return None
    target = CANONICAL.get(dim)
    if target is None:
        return None
    try:
        factor = float(q.to(target.replace("^", "**")).magnitude)
    except Exception:
        return None
    return target, factor


def normalise_value(value: float, unit: str) -> tuple[float, str] | None:
    r = normalise_unit(unit)
    if r is None:
        return None
    canon, factor = r
    return value * factor, canon


__all__ = ["CANONICAL", "LITERAL_UNITS", "normalise_unit", "normalise_value", "registry"]
