# SPDX-License-Identifier: Apache-2.0
"""World prior before and after a library change: ``PriorParams.from_library`` on both files.

``senseai_world.prior`` imports the torch model in its ``__init__``, so this module never
imports the package. It finds world's ``prior/params.py`` (installed package, the stack checkout
``$SENSEAI_STACK_DIR/world``, or ``SENSEAI_WORLD_PARAMS``) and loads that one file, which only
needs PyYAML. If world is nowhere on this machine, the impact record says so instead of guessing.

On world 8ec2cfd, ``from_library`` reads only ``senses[hearing].spatial_resolution``; every other
parameter keeps a flagged default. A vision change therefore reaches the profile, not the world
prior, and the impact report says that in plain words.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

from senseai_research.config import Settings
from senseai_research.ledger.models import WorldRow

_MODULE = "_senseai_research_world_params"


def find_params_file(cfg: Settings) -> Path | None:
    if cfg.world_params is not None and cfg.world_params.is_file():
        return cfg.world_params
    try:
        spec = importlib.util.find_spec("senseai_world")
    except (ImportError, ValueError):
        spec = None
    if spec is not None and spec.submodule_search_locations:
        for loc in spec.submodule_search_locations:
            p = Path(loc) / "prior" / "params.py"
            if p.is_file():
                return p
    p = cfg.stack / "world" / "src" / "senseai_world" / "prior" / "params.py"
    return p if p.is_file() else None


def _load(path: Path) -> ModuleType:
    name = f"{_MODULE}_{abs(hash(str(path)))}"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod  # dataclasses look the module up while building the class
    spec.loader.exec_module(mod)
    return mod


def provenance(params_file: Path, library_file: Path) -> dict[str, dict[str, Any]]:
    mod = _load(params_file)
    pp = mod.PriorParams.from_library(library_file)
    out: dict[str, dict[str, Any]] = pp.provenance()
    return out


def diff(params_file: Path, old_library: Path, new_library: Path) -> tuple[list[WorldRow], bool]:
    """Rows for every prior parameter, and whether the change reaches any of them."""
    a = provenance(params_file, old_library)
    b = provenance(params_file, new_library)
    rows = []
    for name in sorted(set(a) | set(b)):
        pa, pb = a.get(name, {}), b.get(name, {})
        changed = pa.get("value") != pb.get("value") or pa.get("source") != pb.get("source")
        rows.append(
            WorldRow(
                param=name,
                old=float(pa.get("value", float("nan"))),
                new=float(pb.get("value", float("nan"))),
                kind_old=str(pa.get("kind", "")),
                kind_new=str(pb.get("kind", "")),
                source_new=str(pb.get("source", "")),
                measured=pb.get("kind") == "measured",
                changed=changed,
            )
        )
    return rows, any(r.changed for r in rows)


__all__ = ["diff", "find_params_file", "provenance"]
