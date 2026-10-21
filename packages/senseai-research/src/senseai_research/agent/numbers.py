# SPDX-License-Identifier: Apache-2.0
"""N5 guard: performance numbers the agent types must exist in a stored experiment record.

After every assistant turn, sentences that talk about performance (hit rate, chance, delay,
false fires, latency, objects) are scanned for numbers. Each number must match a value from an
``experiment_get`` / ``experiment_run`` result in this conversation (as a fraction, a
percentage, or rounded the way people round). Anything else is flagged to the person and stored
with the message.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Iterable
from typing import Any

PERF = re.compile(
    r"hit rate|chance|luck|delay|false fire|fires? per hour|fires/h|latency|p99|objects|hits",
    re.I,
)
NUM = re.compile(r"(?<![\w.])[-+]?\d+(?:[.,]\d+)?(?:\s?%)?")
SENT = re.compile(r"(?<=[.!?])\s+|\n+")
EXPERIMENT_TOOLS = ("experiment_get", "experiment_run")


def _walk(o: Any) -> Iterable[float]:
    if isinstance(o, bool):
        return
    if isinstance(o, (int, float)):
        if math.isfinite(o):
            yield float(o)
    elif isinstance(o, dict):
        for v in o.values():
            yield from _walk(v)
    elif isinstance(o, list):
        for v in o:
            yield from _walk(v)
    elif isinstance(o, str):
        for m in NUM.finditer(o):
            try:
                yield float(m.group(0).rstrip("% ").replace(",", ""))
            except ValueError:
                continue


def allowed_numbers(messages: list[dict[str, Any]]) -> set[float]:
    """Every number in experiment tool results of this conversation, as stored."""
    names: dict[str, str] = {}
    vals: set[float] = set()
    for m in messages:
        for b in m.get("content", []):
            if b.get("type") == "tool_use":
                names[b["id"]] = b["name"]
            elif (
                b.get("type") == "tool_result"
                and names.get(b.get("tool_use_id", "")) in EXPERIMENT_TOOLS
            ):
                content = b.get("content")
                text = content if isinstance(content, str) else json.dumps(content)
                try:
                    vals.update(_walk(json.loads(text)))
                except ValueError:
                    vals.update(_walk(text))
    return vals


def _matches(x: float, decimals: int, allowed: set[float]) -> bool:
    """``x`` as written (with ``decimals`` places) equals some stored value, or that value x100."""
    tol = 0.5 * 10.0**-decimals + 1e-9
    return any(abs(c - x) <= tol for v in allowed for c in (v, v * 100.0))


def unsupported_numbers(text: str, allowed: set[float]) -> list[str]:
    """Performance numbers in ``text`` that no experiment record in the conversation contains."""
    bad: list[str] = []
    for sentence in SENT.split(text):
        if not PERF.search(sentence):
            continue
        for m in NUM.finditer(sentence):
            raw = m.group(0)
            num = raw.rstrip("% ").replace(",", ".")
            try:
                x = float(num)
            except ValueError:
                continue
            decimals = len(num.split(".", 1)[1]) if "." in num else 0
            if x == 20 and "20" in sentence and "object" in sentence.lower():
                continue  # the "fewer than 20 objects" rule, not a result
            if not _matches(x, decimals, allowed):
                bad.append(raw.strip())
    return bad


__all__ = ["allowed_numbers", "unsupported_numbers"]
