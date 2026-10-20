# SPDX-License-Identifier: Apache-2.0
"""Library path → the profile fields whose rules read it (``research.consumers_by_path``)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class ProfilesTree:
    """Where a profiles checkout keeps what the impact code reads."""

    root: Path

    @property
    def library(self) -> Path:
        return self.root / "library"

    @property
    def schema(self) -> Path:
        return self.root / "library" / "schema" / "clade.schema.json"

    @property
    def specs(self) -> Path:
        return self.root / "packages" / "senseai-profiles" / "profiles"

    @property
    def priors(self) -> Path:
        return self.root / "packages" / "senseai-profiles" / "priors"

    @property
    def sensors(self) -> Path:
        return self.root / "sensors"

    def library_file(self, library_path: str) -> Path:
        return self.library / f"{library_path.split('.')[0]}.yaml"


def load(tree: ProfilesTree, library_dir: Path | None = None) -> tuple[Any, dict[str, Any]]:
    """``(library, specs)`` loaded from the tree (or a patched copy of its library)."""
    from senseai_profiles.library import load_library
    from senseai_profiles.spec import load_specs

    lib = load_library(library_dir or tree.library, schema_path=tree.schema)
    specs: dict[str, Any] = load_specs(tree.specs, lib)
    return lib, specs


def consumers(tree: ProfilesTree, library_path: str) -> list[str]:
    """Labels like ``panther.frame.gain_lut (vision_supply_vs_demand)``, sorted."""
    from senseai_profiles.research import consumers_by_path

    _, specs = load(tree)
    found = consumers_by_path(specs).get(library_path, [])
    return sorted(c.label for c in found)


__all__ = ["ProfilesTree", "consumers", "load"]
