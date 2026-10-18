# SPDX-License-Identifier: Apache-2.0
"""Test double for ``senseai_world/prior/params.py`` (not world's code).

It has the same interface the impact code uses (``PriorParams.from_library(path)`` and
``.provenance()``) and the same behaviour world 8ec2cfd documents: only
``senses[hearing].spatial_resolution`` (deg) is read from the library, as
``sigma_bearing_deg``; every other parameter keeps a flagged default.
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class Param:
    value: float
    kind: str
    source: str


@dataclass(frozen=True)
class PriorParams:
    sigma_bearing_deg: Param = field(default_factory=lambda: Param(5.0, "measured", "default"))
    integration_ticks: Param = field(default_factory=lambda: Param(3, "proposed", "open question"))
    w_loom: Param = field(default_factory=lambda: Param(0.5, "proposed", "no felid value"))
    match_gate: Param = field(default_factory=lambda: Param(9.0, "physics", "3-sigma gate"))

    def provenance(self) -> dict[str, dict[str, Any]]:
        return {
            f.name: {"value": p.value, "kind": p.kind, "source": p.source}
            for f in fields(self)
            for p in [getattr(self, f.name)]
        }

    @classmethod
    def from_library(cls, path: str | Path) -> PriorParams:
        entry = yaml.safe_load(Path(path).read_text()) or {}
        over = {}
        for s in entry.get("senses", []):
            sr = s.get("spatial_resolution")
            if s.get("id") == "hearing" and sr and sr.get("unit") == "deg":
                cites = ", ".join(str(c) for c in sr.get("citations", []))
                over["sigma_bearing_deg"] = Param(
                    float(sr["value"]), "measured", f"hearing.spatial_resolution ({cites})"
                )
        return cls(**over)
