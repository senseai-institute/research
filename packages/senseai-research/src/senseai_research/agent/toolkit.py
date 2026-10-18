# SPDX-License-Identifier: Apache-2.0
"""What every agent tool is built from: the spec, the context, schema helpers, the limiter.

See :mod:`senseai_research.agent.tools` for the registry and the three kinds of tool.
"""

from __future__ import annotations

import json
import time
from collections import defaultdict, deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Literal

import jsonschema

from senseai_research import literature
from senseai_research.config import Settings
from senseai_research.experiments.jobs import ExperimentQueue
from senseai_research.ledger.models import Claim, PendingAction, Source
from senseai_research.ledger.store import Store

Kind = Literal["read", "compute", "write"]
Emit = Callable[[str, dict[str, Any]], None]


class ToolError(Exception):
    """Returned to the model as an ``is_error`` tool result, in plain words."""


@dataclass
class ToolContext:
    store: Store
    cfg: Settings
    conversation_id: str
    emit: Emit = lambda _e, _d: None
    queue: ExperimentQueue | None = None
    actor: str = ""
    experiment_wait_s: float = 1800.0
    extract_call: Any = None  # curate ModelCall override (tests)
    offline: bool | None = None

    def __post_init__(self) -> None:
        if not self.actor:
            self.actor = f"agent:{self.conversation_id}"


@dataclass
class ToolOutput:
    result: dict[str, Any]
    summary: str
    pending: PendingAction | None = None


Handler = Callable[[ToolContext, dict[str, Any]], ToolOutput]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    kind: Kind
    description: str
    properties: Mapping[str, Any]
    handler: Handler
    label: Callable[[dict[str, Any]], str]
    rate: tuple[int, float] | None = None  # (calls, per seconds) for compute tools

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "additionalProperties": False,
            "required": sorted(self.properties),
            "properties": dict(self.properties),
        }

    def api(self) -> dict[str, Any]:
        tool = {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
            "strict": True,
            "eager_input_streaming": True,
        }
        out: dict[str, Any] = json.loads(json.dumps(tool, sort_keys=True))
        return out


# --- schema helpers ------------------------------------------------------------------------


def _s(desc: str) -> dict[str, Any]:
    return {"type": "string", "description": desc}


def _null(schema: dict[str, Any], desc: str) -> dict[str, Any]:
    """Nullable via ``anyOf`` (strict tool schemas support anyOf, not type arrays)."""
    return {"anyOf": [schema, {"type": "null"}], "description": desc}


def _ns(desc: str) -> dict[str, Any]:
    return _null({"type": "string"}, desc)


def _nn(desc: str) -> dict[str, Any]:
    return _null({"type": "number"}, desc)


def _ni(desc: str) -> dict[str, Any]:
    return _null({"type": "integer"}, desc)


def _strs(desc: str) -> dict[str, Any]:
    return {"type": "array", "items": {"type": "string"}, "description": desc}


def _nstrs(desc: str) -> dict[str, Any]:
    return _null({"type": "array", "items": {"type": "string"}}, desc)


def _enum_n(values: tuple[str, ...], desc: str) -> dict[str, Any]:
    return _null({"type": "string", "enum": list(values)}, desc)


# --- presentation helpers ------------------------------------------------------------------


def _claim_brief(c: Claim) -> dict[str, Any]:
    return {
        "claim_id": c.id,
        "library_path": c.library_path,
        "value": c.quantity.text(),
        "status": c.status,
        "species": c.species,
        "conditions": c.conditions,
        "revision": c.revision,
    }


def _source_brief(s: Source) -> dict[str, Any]:
    return {
        "source_id": s.id,
        "doi": s.doi,
        "url": s.url,
        "title": s.title,
        "authors": s.authors,
        "year": s.year,
        "venue": s.venue,
        "full_text_in_ledger": bool(s.fetched_text_sha256 and s.fulltext_available),
        "how_much_was_read_before": s.checked,
    }


def _client(ctx: ToolContext) -> Any:
    return literature.client(ctx.cfg.cache_dir / "curate", offline=ctx.offline)


def _words(*parts: Any) -> str:
    return " ".join(str(x) for x in parts if x)


def _quantity_arg(a: Mapping[str, Any]) -> dict[str, Any]:
    q = {k: a.get(k) for k in ("value", "low", "high", "unit")}
    return {k: v for k, v in q.items() if v is not None}


_Q = {
    "value": _nn("new single value, in the library's unit (null for a range)"),
    "low": _nn("range low end (null for a single value)"),
    "high": _nn("range high end (null for a single value)"),
    "unit": _ns("unit; must be the library's unit for this path (null = the library's)"),
}


class RateLimiter:
    def __init__(self) -> None:
        self._calls: dict[tuple[str, str], deque[float]] = defaultdict(deque)

    def allow(self, conversation_id: str, spec: ToolSpec) -> bool:
        if spec.rate is None:
            return True
        n, per = spec.rate
        q = self._calls[(conversation_id, spec.name)]
        t = time.monotonic()
        while q and t - q[0] > per:
            q.popleft()
        if len(q) >= n:
            return False
        q.append(t)
        return True


def validate(spec: ToolSpec, args: Any) -> None:
    """Strict-schema check of a (possibly eagerly streamed) input before anything runs."""
    jsonschema.Draft202012Validator(spec.input_schema).validate(args)


__all__ = [
    "Emit",
    "Handler",
    "Kind",
    "RateLimiter",
    "ToolContext",
    "ToolError",
    "ToolOutput",
    "ToolSpec",
    "validate",
]
