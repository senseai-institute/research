# SPDX-License-Identifier: Apache-2.0
"""The agent's tools (spec §7.2): strict JSON-schema definitions and their handlers.

Three kinds:

* **read** tools run freely and change nothing;
* **compute** tools run freely within rate limits; they may fetch papers, run the extraction
  pipeline, store impact records or start bench jobs, but never change a claim's value or status
  and never touch the library;
* **write** tools never execute a change. They create a pending action
  (:mod:`senseai_research.actions`) and the loop ends the turn with a card a person must approve
  or reject (N1).

There is no tool that edits files, runs shell commands or calls GitHub.

The list sent to the API is sorted by name with sorted keys, so it is byte-identical on every
request (prompt caching). Every input is validated against its schema before a handler runs.
"""

from __future__ import annotations

from typing import Any

from senseai_research.agent.toolkit import (
    Emit,
    Handler,
    Kind,
    RateLimiter,
    ToolContext,
    ToolError,
    ToolOutput,
    ToolSpec,
    validate,
)
from senseai_research.agent.tools_read import READ_TOOLS

TOOLS: tuple[ToolSpec, ...] = (*READ_TOOLS,)  # compute and write tools: next PR
BY_NAME: dict[str, ToolSpec] = {t.name: t for t in TOOLS}


def api_tools() -> list[dict[str, Any]]:
    """The tool list for the API: sorted by name, sorted keys, byte-stable."""
    return [BY_NAME[n].api() for n in sorted(BY_NAME)]


__all__ = [
    "BY_NAME",
    "TOOLS",
    "Emit",
    "Handler",
    "Kind",
    "RateLimiter",
    "ToolContext",
    "ToolError",
    "ToolOutput",
    "ToolSpec",
    "api_tools",
    "validate",
]
