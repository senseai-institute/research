# SPDX-License-Identifier: Apache-2.0
"""Edit one quantity in a library entry's YAML, keeping comments and layout (ruamel round-trip).

Paths are library paths (``felidae.senses.vision.detection_threshold``,
``felidae.senses.vision.measurements.rod_density_peak``,
``felidae.behaviours.pounce.latency_ms``). Only the quantity's own keys change; the rest of
the file is written back as it was.
"""

from __future__ import annotations

import difflib
import hashlib
import io
from collections.abc import Mapping
from typing import Any

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq

QUANTITY_KEYS = (
    "value",
    "unit",
    "confidence",
    "species",
    "conditions",
    "notes",
    "citations",
    "evidence",
)


class LibraryEditError(ValueError):
    pass


def _yaml() -> YAML:
    y = YAML()
    y.preserve_quotes = True
    y.width = 4096  # never re-wrap: the PR diff must show only the changed value
    y.indent(mapping=2, sequence=4, offset=2)
    return y


def _find(items: Any, key: str, ident: str, what: str) -> CommentedMap:
    if not isinstance(items, CommentedSeq):
        raise LibraryEditError(f"no {what} list")
    for it in items:
        if isinstance(it, CommentedMap) and it.get(key) == ident:
            return it
    raise LibraryEditError(f"no {what} {ident!r} in the entry")


def locate(doc: CommentedMap, path: str) -> CommentedMap:
    """The mapping that holds the quantity at ``path``."""
    parts = path.split(".")
    if len(parts) < 4:
        raise LibraryEditError(f"{path!r} is not a value path")
    _, section, ident, *rest = parts
    if section == "senses":
        item = _find(doc.get("senses"), "id", ident, "sense")
    elif section == "behaviours":
        item = _find(doc.get("behaviours"), "name", ident, "behaviour")
    else:
        raise LibraryEditError(f"section must be senses or behaviours, not {section!r}")
    if rest[0] == "measurements":
        if len(rest) != 2:
            raise LibraryEditError("a measurement path names exactly one measurement")
        return _find(item.get("measurements"), "id", rest[1], "measurement")
    if len(rest) != 1:
        raise LibraryEditError(f"unexpected segments in {path!r}")
    q = item.get(rest[0])
    if not isinstance(q, CommentedMap):
        raise LibraryEditError(f"{path!r} is not set in the entry")
    return q


def load(text: str) -> CommentedMap:
    doc = _yaml().load(text)
    if not isinstance(doc, CommentedMap):
        raise LibraryEditError("not a library entry")
    return doc


def dump(doc: CommentedMap) -> str:
    buf = io.StringIO()
    _yaml().dump(doc, buf)
    return buf.getvalue()


def _plain(v: Any) -> Any:
    if isinstance(v, list):
        seq = CommentedSeq(_plain(x) for x in v)
        if all(isinstance(x, (int, float)) for x in v):
            seq.fa.set_flow_style()
        return seq
    return v


def set_quantity(text: str, path: str, changes: Mapping[str, Any]) -> str:
    """Return ``text`` with the quantity at ``path`` updated (``None`` deletes a key)."""
    bad = set(changes) - set(QUANTITY_KEYS)
    if bad:
        raise LibraryEditError(f"not quantity fields: {sorted(bad)}")
    doc = load(text)
    q = locate(doc, path)
    for k, v in changes.items():
        if v is None:
            q.pop(k, None)
        else:
            q[k] = _plain(v)
    return dump(doc)


def get_quantity(text: str, path: str) -> dict[str, Any]:
    q = locate(load(text), path)
    return {k: (list(v) if isinstance(v, CommentedSeq) else v) for k, v in q.items()}


def unified_diff(old: str, new: str, filename: str) -> str:
    return "".join(
        difflib.unified_diff(
            old.splitlines(keepends=True),
            new.splitlines(keepends=True),
            fromfile=f"a/{filename}",
            tofile=f"b/{filename}",
        )
    )


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


__all__ = [
    "QUANTITY_KEYS",
    "LibraryEditError",
    "dump",
    "get_quantity",
    "load",
    "locate",
    "set_quantity",
    "sha",
    "unified_diff",
]
