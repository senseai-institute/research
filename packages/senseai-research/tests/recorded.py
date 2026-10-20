# SPDX-License-Identifier: Apache-2.0
"""Recorded Anthropic responses, replayed through the real SDK (spec §13: no key, no network).

A fixture is a list of final messages in the API's JSON shape (what ``RESEARCH_RECORD_DIR``
saves from a live run, or written by hand in that shape). :class:`RecordedTransport` serves them
in order as a Server-Sent Events stream, so ``client.beta.messages.stream`` parses them exactly
as it parses the live service. Every request is checked against the spec's model call (model,
thinking, effort, fallbacks, caching, strict tools), against a byte-stable system prompt and
tool list, and for an append-only history (each request's messages extend the previous
request's). ``{{name}}`` in a recorded message is replaced by the latest value of ``name`` in
a JSON tool result of the conversation (ids made at run time, such as experiment ids).
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import anthropic
import httpx2 as httpx  # anthropic 1.x uses httpx2 (the maintained httpx fork)

PLACEHOLDER = re.compile(r"\{\{(\w+)\}\}")


def _sse(event: str, data: dict[str, Any]) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n".encode()


def _chunks(s: str, n: int = 40) -> Iterator[str]:
    for i in range(0, len(s), n):
        yield s[i : i + n]


def message_events(msg: dict[str, Any]) -> bytes:
    usage = msg.get("usage", {})
    out = [
        _sse(
            "message_start",
            {
                "type": "message_start",
                "message": {
                    "id": msg.get("id", "msg_recorded"),
                    "type": "message",
                    "role": "assistant",
                    "model": msg.get("model", "claude-opus-5-5"),
                    "content": [],
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {
                        "input_tokens": usage.get("input_tokens", 0),
                        "output_tokens": 1,
                        "cache_read_input_tokens": usage.get("cache_read_input_tokens", 0),
                        "cache_creation_input_tokens": usage.get("cache_creation_input_tokens", 0),
                    },
                },
            },
        )
    ]
    for i, b in enumerate(msg["content"]):
        t = b["type"]
        if t == "text":
            out.append(
                _sse(
                    "content_block_start",
                    {
                        "type": "content_block_start",
                        "index": i,
                        "content_block": {"type": "text", "text": ""},
                    },
                )
            )
            for c in _chunks(b["text"]):
                out.append(
                    _sse(
                        "content_block_delta",
                        {
                            "type": "content_block_delta",
                            "index": i,
                            "delta": {"type": "text_delta", "text": c},
                        },
                    )
                )
        elif t == "thinking":
            out.append(
                _sse(
                    "content_block_start",
                    {
                        "type": "content_block_start",
                        "index": i,
                        "content_block": {"type": "thinking", "thinking": "", "signature": ""},
                    },
                )
            )
            for c in _chunks(b.get("thinking", "")):
                out.append(
                    _sse(
                        "content_block_delta",
                        {
                            "type": "content_block_delta",
                            "index": i,
                            "delta": {"type": "thinking_delta", "thinking": c},
                        },
                    )
                )
            out.append(
                _sse(
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": i,
                        "delta": {
                            "type": "signature_delta",
                            "signature": b.get("signature", "sig"),
                        },
                    },
                )
            )
        elif t == "tool_use":
            out.append(
                _sse(
                    "content_block_start",
                    {
                        "type": "content_block_start",
                        "index": i,
                        "content_block": {
                            "type": "tool_use",
                            "id": b["id"],
                            "name": b["name"],
                            "input": {},
                        },
                    },
                )
            )
            for c in _chunks(json.dumps(b["input"]), 25):
                out.append(
                    _sse(
                        "content_block_delta",
                        {
                            "type": "content_block_delta",
                            "index": i,
                            "delta": {"type": "input_json_delta", "partial_json": c},
                        },
                    )
                )
        else:
            raise ValueError(f"recorded block type {t!r} is not supported by the replayer")
        out.append(_sse("content_block_stop", {"type": "content_block_stop", "index": i}))
    out.append(
        _sse(
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": msg["stop_reason"], "stop_sequence": None},
                "usage": {"output_tokens": usage.get("output_tokens", 10)},
            },
        )
    )
    out.append(_sse("message_stop", {"type": "message_stop"}))
    return b"".join(out)


class RecordedTransport(httpx.BaseTransport):
    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self.responses = list(responses)
        self.requests: list[dict[str, Any]] = []
        self.headers: list[httpx.Headers] = []
        self.problems: list[str] = []
        self.values: dict[str, Any] = {}

    @classmethod
    def from_file(cls, path: Path) -> RecordedTransport:
        return cls(json.loads(path.read_text())["responses"])

    def _check(self, body: dict[str, Any], headers: httpx.Headers) -> None:
        def need(cond: bool, what: str) -> None:
            if not cond:
                self.problems.append(f"request {len(self.requests)}: {what}")

        need(body.get("model") == "claude-opus-5-5", f"model {body.get('model')}")
        need(body.get("max_tokens") == 64000, "max_tokens")
        need(body.get("thinking") == {"type": "adaptive", "display": "summarized"}, "thinking")
        need(body.get("output_config") == {"effort": "high"}, "effort")
        need(body.get("fallbacks") == "default", "fallbacks")
        need(body.get("cache_control") == {"type": "ephemeral"}, "cache_control")
        need(body.get("stream") is True, "stream")
        need("server-side-fallback-2026-07-01" in headers.get("anthropic-beta", ""), "beta header")
        tools = body.get("tools", [])
        need([t["name"] for t in tools] == sorted(t["name"] for t in tools), "tools sorted")
        need(
            all(t.get("strict") is True and t.get("eager_input_streaming") is True for t in tools),
            "strict+eager",
        )
        if self.requests:
            prev = self.requests[-1]
            need(body["system"] == prev["system"], "system prompt changed between requests")
            need(json.dumps(body["tools"]) == json.dumps(prev["tools"]), "tool list changed")
            pm, cm = prev["messages"], body["messages"]
            same_conv = cm and pm and cm[0] == pm[0]
            if same_conv:
                # append-only: every earlier message is unchanged; the last earlier one may only
                # have gained blocks at its end (consecutive turns of one role are joined)
                ok = len(cm) >= len(pm) and cm[: len(pm) - 1] == pm[:-1]
                last_p, last_c = pm[-1], cm[len(pm) - 1] if len(cm) >= len(pm) else None
                ok = ok and last_c is not None and last_c["role"] == last_p["role"]
                ok = ok and last_c["content"][: len(last_p["content"])] == last_p["content"]
                need(bool(ok), "history was edited (not append-only)")

    def _collect(self, body: dict[str, Any]) -> None:
        for m in body["messages"]:
            if not isinstance(m["content"], list):
                continue
            for b in m["content"]:
                if b.get("type") == "tool_result" and not b.get("is_error"):
                    try:
                        val = json.loads(b["content"]) if isinstance(b["content"], str) else None
                    except ValueError:
                        val = None
                    if isinstance(val, dict):
                        for k, v in val.items():
                            if isinstance(v, (str, int, float)):
                                self.values[k] = v

    def _fill(self, msg: dict[str, Any]) -> dict[str, Any]:
        text = json.dumps(msg)

        def sub(m: re.Match[str]) -> str:
            v = self.values.get(m.group(1))
            if v is None:
                self.problems.append(f"no value for placeholder {m.group(0)}")
                return m.group(0)
            return str(v)

        return dict(json.loads(PLACEHOLDER.sub(sub, text)))

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self._check(body, request.headers)
        self.requests.append(body)
        self.headers.append(request.headers)
        self._collect(body)
        if not self.responses:
            self.problems.append("the replay ran out of recorded responses")
            return httpx.Response(
                500,
                json={
                    "type": "error",
                    "error": {"type": "api_error", "message": "no more recordings"},
                },
            )
        msg = self._fill(self.responses.pop(0))
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=message_events(msg),
        )


def factory(transport: RecordedTransport) -> Any:
    def make() -> anthropic.Anthropic:
        return anthropic.Anthropic(
            api_key="test-key",
            base_url="http://recorded.test",
            http_client=httpx.Client(transport=transport),
            max_retries=0,
        )

    return make
