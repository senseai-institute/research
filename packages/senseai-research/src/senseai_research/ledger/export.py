# SPDX-License-Identifier: Apache-2.0
"""Write the ledger as reviewable YAML (one file per clade for claims, one per other kind).

The output is deterministic: records sorted by id, keys in model order, no export timestamp.
Exporting the same ledger twice gives byte-identical files, so the export can be committed
and diffed. Each claim carries its full revision history (N3).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from senseai_research.ledger.models import Claim
from senseai_research.ledger.store import Store

KINDS = ("source", "passage", "hypothesis", "experiment", "impact", "proposal", "approval")


def plural(kind: str) -> str:
    return "hypotheses" if kind == "hypothesis" else kind + "s"


class _Dumper(yaml.SafeDumper):
    pass


def _str(d: yaml.SafeDumper, s: str) -> yaml.ScalarNode:
    style = "|" if "\n" in s else None
    return d.represent_scalar("tag:yaml.org,2002:str", s, style=style)


_Dumper.add_representer(str, _str)


def dump(data: Any) -> str:
    return yaml.dump(data, Dumper=_Dumper, sort_keys=False, allow_unicode=True, width=100)


def claim_doc(store: Store, c: Claim) -> dict[str, Any]:
    d = c.model_dump()
    d["history"] = [r.model_dump() for r in store.claim_history(c.id)]
    return d


def export(store: Store, out: Path) -> list[Path]:
    """Write ``claims/<clade>.yaml`` and ``<kind>s.yaml``; returns the files written."""
    out.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    by_clade: dict[str, list[dict[str, Any]]] = {}
    for c in sorted(store.claims(), key=lambda c: c.id):
        by_clade.setdefault(c.clade, []).append(claim_doc(store, c))
    (out / "claims").mkdir(exist_ok=True)
    for clade, docs in sorted(by_clade.items()):
        p = out / "claims" / f"{clade}.yaml"
        p.write_text(dump({"clade": clade, "claims": docs}))
        written.append(p)
    for kind in KINDS:
        p = out / f"{plural(kind)}.yaml"
        p.write_text(dump({plural(kind): store.list_raw(kind)}))
        written.append(p)
    return written


def load_export(root: Path) -> dict[str, Any]:
    """Read an export back (for review tooling and the round-trip test)."""
    out: dict[str, Any] = {"claims": {}}
    for p in sorted((root / "claims").glob("*.yaml")):
        doc = yaml.safe_load(p.read_text())
        out["claims"][doc["clade"]] = doc["claims"]
    for kind in KINDS:
        p = root / f"{plural(kind)}.yaml"
        if p.exists():
            out[plural(kind)] = yaml.safe_load(p.read_text())[plural(kind)]
    return out


__all__ = ["KINDS", "claim_doc", "dump", "export", "load_export", "plural"]
