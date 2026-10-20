# SPDX-License-Identifier: Apache-2.0
"""Run a blocking agent turn in a thread and stream its events as Server-Sent Events.

The agent loop calls ``emit(kind, data)`` from its own thread; :func:`stream_turn` turns those
calls into SSE messages (``event: <kind>``, ``data: <json>``) for the response, in order, and
ends after ``done``. If the browser goes away the turn still finishes and is stored; only the
stream stops.
"""

from __future__ import annotations

import asyncio
import json
import queue
import threading
from collections.abc import AsyncIterator, Callable
from typing import Any

Emit = Callable[[str, dict[str, Any]], None]
EVENTS = (
    "text_delta",
    "thinking_summary",
    "tool_call",
    "tool_result_summary",
    "pending_action",
    "job_progress",
    "done",
    "error",
)
_END = object()


def sse_line(kind: str, data: dict[str, Any]) -> dict[str, str]:
    return {"event": kind, "data": json.dumps(data, ensure_ascii=False, default=str)}


async def stream_turn(run: Callable[[Emit], Any]) -> AsyncIterator[dict[str, str]]:
    """``run(emit)`` happens in a worker thread; its events are yielded as SSE messages."""
    q: queue.Queue[Any] = queue.Queue()

    def emit(kind: str, data: dict[str, Any]) -> None:
        q.put((kind, data))

    def work() -> None:
        try:
            run(emit)
        except Exception as e:  # report, then end the stream
            q.put(("error", {"message": f"The assistant stopped: {type(e).__name__}: {e}"}))
            q.put(("done", {"stop": "error", "pending": [], "usage": {}}))
        finally:
            q.put(_END)

    threading.Thread(target=work, daemon=True, name="agent-turn").start()
    while True:
        item = await asyncio.to_thread(q.get)
        if item is _END:
            return
        kind, data = item
        yield sse_line(kind, data)


__all__ = ["EVENTS", "sse_line", "stream_turn"]
