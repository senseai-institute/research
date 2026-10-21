# SPDX-License-Identifier: Apache-2.0
"""World prior before and after a library change: ``PriorParams.from_library`` on both files.

The token-space prior now lives in senseai-circuits (``senseai_circuits.tokenspace``, NumPy and
PyYAML only); senseai-world re-exports it, so world's ``prior/params.py`` is a one-line import of
``senseai_circuits`` and no longer loads on its own. This module looks, in order, for
``SENSEAI_WORLD_PARAMS``, an installed senseai-circuits (``tokenspace.py``), and world's own
``prior/params.py`` (installed package or the stack checkout ``$SENSEAI_STACK_DIR/world``) when
that file still defines the prior itself. ``senseai_world.prior`` imports the torch model in its
``__init__``, so the package itself is never imported. If no usable file is on this machine, the
impact record says so instead of guessing.

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


def _package_dirs(name: str) -> list[Path]:
    try:
        spec = importlib.util.find_spec(name)
    except (ImportError, ValueError):
        spec = None
    if spec is None or not spec.submodule_search_locations:
        return []
    return [Path(loc) for loc in spec.submodule_search_locations]


def _reexports_circuits(path: Path) -> bool:
    """World after world#10: its params.py only re-exports senseai_circuits.tokenspace."""
    try:
        return "senseai_circuits" in path.read_text(encoding="utf-8")
    except OSError:
        return False


def find_params_file(cfg: Settings) -> Path | None:
    if cfg.world_params is not None and cfg.world_params.is_file():
        return cfg.world_params
    for d in _package_dirs("senseai_circuits"):
        p = d / "tokenspace.py"
        if p.is_file():
            return p
    candidates = [d / "prior" / "params.py" for d in _package_dirs("senseai_world")]
    candidates.append(cfg.stack / "world" / "src" / "senseai_world" / "prior" / "params.py")
    for p in candidates:
        # a re-exporting params.py needs senseai-circuits, which was not found above
        if p.is_file() and not _reexports_circuits(p):
            return p
    return None


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
