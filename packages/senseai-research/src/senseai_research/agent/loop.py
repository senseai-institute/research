# SPDX-License-Identifier: Apache-2.0
"""The agent loop (spec D5, §7.1): a manual streaming loop on the Anthropic Python SDK.

One call to :meth:`AgentLoop.run_turn` handles one user message:

1. the user message is appended to the transcript (append-only);
2. ``client.beta.messages.stream`` is called with the frozen system prompt and tool list,
   adaptive thinking (summarised), effort ``high``, top-level prompt caching and server-side
   ``fallbacks: "default"``; text and thinking stream out as events;
3. the final message's content is stored unchanged;
4. ``stop_reason`` is checked **before** any tool runs: ``refusal`` and ``max_tokens`` end the
   turn without running tools, and ``pause_turn`` is resumed by re-sending;
5. tool inputs are validated against their strict schemas, then read and compute tools run.
   Write tools only create pending actions, and when one was created the turn ends there, with
   the card shown and the model not called again (N1);
6. errors go back to the model as ``tool_result`` blocks with ``is_error: true``.

Events (the SSE contract, spec §8): ``text_delta``, ``thinking_summary``, ``tool_call``,
``tool_result_summary``, ``pending_action``, ``job_progress``, ``done``, ``error``.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import anthropic

from senseai_research.agent import numbers
from senseai_research.agent.client import NO_KEY, make_client, missing_credentials
from senseai_research.agent.prompts import SYSTEM_PROMPT
from senseai_research.agent.tools import (
    BY_NAME,
    RateLimiter,
    ToolContext,
    ToolError,
    api_tools,
    validate,
)
from senseai_research.agent.transcript import Transcript
from senseai_research.config import Settings
from senseai_research.experiments.jobs import ExperimentQueue
from senseai_research.ledger.store import Store

MAX_TOKENS = 64000
EFFORT = "high"
BETAS = ["server-side-fallback-2026-07-01"]
THINKING = {"type": "adaptive", "display": "summarized"}
MAX_STEPS = 40  # model calls per user message
MAX_PAUSES = 5
MAX_JSON_RETRIES = 2
#: USD per million tokens for claude-opus-5-5 (input, output, cache read, 5-minute cache write).
PRICES = {"input": 4.0, "output": 20.0, "cache_read": 0.20, "cache_write": 5.0}

Emit = Callable[[str, dict[str, Any]], None]


def api_reason(e: anthropic.APIStatusError) -> str:
    """The reason the API gave (``error.message`` of its body), shortened; never a credential."""
    body = e.body if isinstance(e.body, dict) else {}
    err = body.get("error")
    reason = str(err.get("message") or "").strip() if isinstance(err, dict) else ""
    return reason if len(reason) <= 300 else reason[:297] + "..."


def status_message(e: anthropic.APIStatusError) -> str:
    """What the chat shows when the API answers with an error status."""
    reason = api_reason(e)
    msg = f"The model service answered {e.status_code}"
    msg += f": {reason}" if reason else "."
    if "credit balance" in reason.lower():
        msg += " Add credits at https://console.anthropic.com/settings/billing."
    elif e.status_code in (401, 403):
        msg += " Check ANTHROPIC_API_KEY on the research server."
    elif e.status_code == 404 or "model" in reason.lower():
        msg += " Check RESEARCH_MODEL on the research server (unset uses the default)."
    return msg


def request_params(cfg: Settings, messages: list[dict[str, Any]]) -> dict[str, Any]:
    """Everything sent to ``client.beta.messages.stream`` (also used to check request shape)."""
    return {
        "model": cfg.model,
        "max_tokens": MAX_TOKENS,
        "thinking": THINKING,
        "output_config": {"effort": EFFORT},
        "betas": BETAS,
        "fallbacks": "default",
        "cache_control": {"type": "ephemeral"},
        "system": SYSTEM_PROMPT,
        "tools": api_tools(),
        "messages": messages,
    }


@dataclass
class Usage:
    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write: int = 0
    calls: int = 0

    def add(self, u: Any) -> None:
        self.calls += 1
        self.input += int(getattr(u, "input_tokens", 0) or 0)
        self.output += int(getattr(u, "output_tokens", 0) or 0)
        self.cache_read += int(getattr(u, "cache_read_input_tokens", 0) or 0)
        self.cache_write += int(getattr(u, "cache_creation_input_tokens", 0) or 0)

    @property
    def cost_usd(self) -> float:
        return (
            self.input * PRICES["input"]
            + self.output * PRICES["output"]
            + self.cache_read * PRICES["cache_read"]
            + self.cache_write * PRICES["cache_write"]
        ) / 1e6

    def as_dict(self) -> dict[str, Any]:
        return {
            "model_calls": self.calls,
            "input_tokens": self.input,
            "output_tokens": self.output,
            "cache_read_input_tokens": self.cache_read,
            "cache_creation_input_tokens": self.cache_write,
            "cost_usd": round(self.cost_usd, 6),
        }


#: Small fields of a tool result the page uses to open the right context tab (not raw JSON).
REF_KEYS = (
    "claim_id",
    "impact_id",
    "experiment_id",
    "source_id",
    "pending_action_id",
    "doi",
    "section",
    "text",
    "context_before",
    "context_after",
)


def refs(result: dict[str, Any]) -> dict[str, Any]:
    return {k: result[k] for k in REF_KEYS if isinstance(result.get(k), (str, int, float))}


def _block(b: Any) -> dict[str, Any]:
    out: dict[str, Any] = b.model_dump(mode="json", exclude_none=True)
    return out


@dataclass
class TurnResult:
    stop: str  # end_turn | pending_action | refusal | max_tokens | error | step_limit
    text: str = ""
    pending: list[str] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    unsupported_numbers: list[str] = field(default_factory=list)


class AgentLoop:
    def __init__(
        self,
        store: Store,
        cfg: Settings,
        *,
        queue: ExperimentQueue | None = None,
        extract_call: Any = None,
        offline: bool | None = None,
    ) -> None:
        self.store = store
        self.cfg = cfg
        self.transcript = Transcript(store)
        self.queue = queue
        self.limiter = RateLimiter()
        self.extract_call = extract_call
        self.offline = offline
        self.record_dir = os.environ.get("RESEARCH_RECORD_DIR")

    def _ctx(self, conversation_id: str, emit: Emit) -> ToolContext:
        if self.queue is None:
            self.queue = ExperimentQueue(self.store, self.cfg)
        return ToolContext(
            store=self.store,
            cfg=self.cfg,
            conversation_id=conversation_id,
            emit=emit,
            queue=self.queue,
            extract_call=self.extract_call,
            offline=self.offline,
        )

    def _record(self, conversation_id: str, step: int, message: Any) -> None:
        """With RESEARCH_RECORD_DIR set, keep each final message (to build replay fixtures)."""
        if not self.record_dir:
            return
        d = Path(self.record_dir) / conversation_id
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{step:03d}.json").write_text(
            json.dumps(message.model_dump(mode="json", exclude_none=True), indent=1)
        )

    def run_turn(
        self,
        conversation_id: str,
        text: str,
        *,
        emit: Emit,
        focus: dict[str, Any] | None = None,
        user: str = "user:senseai",
    ) -> TurnResult:
        content: list[dict[str, Any]] = [{"type": "text", "text": text}]
        if focus:
            what = focus.get("claim_id") or focus.get("library_path")
            if what:
                content.append(
                    {"type": "text", "text": f"(The person has {what} open in the side panel.)"}
                )
        self.transcript.append(conversation_id, "user", content, {"by": user})
        return self.continue_turn(conversation_id, emit=emit)

    def continue_turn(self, conversation_id: str, *, emit: Emit) -> TurnResult:
        ctx = self._ctx(conversation_id, emit)
        client = make_client()
        res = TurnResult(stop="end_turn")
        pauses = json_retries = 0
        texts: list[str] = []
        for step in range(MAX_STEPS):
            messages = self.transcript.api_messages(conversation_id)
            try:
                with client.beta.messages.stream(**request_params(self.cfg, messages)) as stream:
                    for event in stream:
                        self._forward(event, emit)
                    final = stream.get_final_message()
                json_retries = 0
            except ValueError:
                # Tool input JSON the SDK could not parse at all (eager input streaming): the
                # tool_use block never completed, so there is no id to answer. Re-issue.
                json_retries += 1
                if json_retries > MAX_JSON_RETRIES:
                    emit("error", {"message": "The model sent a malformed tool call twice."})
                    res.stop = "error"
                    break
                continue
            except anthropic.APIStatusError as e:
                emit("error", {"message": status_message(e), "code": f"http_{e.status_code}"})
                res.stop = "error"
                break
            except anthropic.APIConnectionError:
                emit("error", {"message": "Could not reach the model service."})
                res.stop = "error"
                break
            except TypeError as e:
                if not missing_credentials(e):
                    raise
                emit("error", {"message": NO_KEY, "code": "no_api_key"})
                res.stop = "error"
                break
            self._record(conversation_id, step, final)
            res.usage.add(final.usage)
            blocks = [_block(b) for b in final.content]
            self.transcript.append(
                conversation_id,
                "assistant",
                blocks,
                {
                    "stop_reason": final.stop_reason,
                    "model": final.model,
                    "usage": res.usage.as_dict(),
                },
            )
            texts += [b.text for b in final.content if b.type == "text"]
            tool_uses = [b for b in final.content if b.type == "tool_use"]
            if final.stop_reason == "refusal":
                emit(
                    "error",
                    {"message": "The model declined to continue this request.", "code": "refusal"},
                )
                res.stop = "refusal"
                break
            if final.stop_reason == "pause_turn":
                pauses += 1
                if pauses > MAX_PAUSES:
                    res.stop = "error"
                    break
                continue
            if final.stop_reason == "max_tokens":
                emit(
                    "error",
                    {
                        "message": "The answer was cut off at the length limit.",
                        "code": "max_tokens",
                    },
                )
                res.stop = "max_tokens"  # a truncated tool input is never run
                break
            if not tool_uses:
                res.stop = "end_turn"
                break
            results, pending = self._run_tools(ctx, tool_uses, emit)
            self.transcript.append(conversation_id, "user", results, {"tool_results": True})
            if pending:
                res.stop = "pending_action"
                res.pending = pending
                break
        else:
            res.stop = "step_limit"
        res.text = "\n\n".join(t for t in texts if t)
        allowed = numbers.allowed_numbers(self.transcript.api_messages(conversation_id))
        res.unsupported_numbers = numbers.unsupported_numbers(res.text, allowed)
        if res.unsupported_numbers:
            emit(
                "error",
                {
                    "code": "unverified_number",
                    "message": (
                        "Check these numbers: they are not in any experiment record of this "
                        f"conversation: {', '.join(res.unsupported_numbers)}."
                    ),
                    "numbers": res.unsupported_numbers,
                },
            )
            self.store.append(
                "agent.unverified_numbers",
                f"agent:{conversation_id}",
                {"conversation_id": conversation_id, "numbers": res.unsupported_numbers},
            )
        emit("done", {"stop": res.stop, "pending": res.pending, "usage": res.usage.as_dict()})
        return res

    def _forward(self, event: Any, emit: Emit) -> None:
        t = getattr(event, "type", "")
        if t == "text":
            emit("text_delta", {"text": event.text})
        elif t == "thinking":
            emit("thinking_summary", {"text": event.thinking})

    def _run_tools(
        self, ctx: ToolContext, tool_uses: list[Any], emit: Emit
    ) -> tuple[list[dict[str, Any]], list[str]]:
        results: list[dict[str, Any]] = []
        pending: list[str] = []
        for tu in tool_uses:
            spec = BY_NAME.get(tu.name)
            args = tu.input if isinstance(tu.input, dict) else {}
            label = spec.label(args) if spec else tu.name
            emit(
                "tool_call",
                {"id": tu.id, "name": tu.name, "label": label, "kind": spec.kind if spec else ""},
            )
            try:
                if spec is None:
                    raise ToolError(f"there is no tool {tu.name!r}")
                try:
                    validate(spec, tu.input)
                except Exception as e:
                    results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": tu.id,
                            "is_error": True,
                            "content": json.dumps({"INVALID_JSON": json.dumps(tu.input)}),
                        }
                    )
                    emit(
                        "tool_result_summary",
                        {
                            "id": tu.id,
                            "ok": False,
                            "summary": f"The request was malformed ({type(e).__name__}).",
                        },
                    )
                    continue
                if not self.limiter.allow(ctx.conversation_id, spec):
                    raise ToolError(
                        "this tool is rate-limited; wait a few minutes before using it again"
                    )
                out = spec.handler(ctx, {**args, "_tool_use_id": tu.id})
            except ToolError as e:
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": tu.id,
                        "is_error": True,
                        "content": str(e),
                    }
                )
                emit("tool_result_summary", {"id": tu.id, "ok": False, "summary": str(e)})
                continue
            except Exception as e:  # a tool bug must not end the conversation
                msg = f"the tool failed: {type(e).__name__}: {e}"
                results.append(
                    {"type": "tool_result", "tool_use_id": tu.id, "is_error": True, "content": msg}
                )
                emit("tool_result_summary", {"id": tu.id, "ok": False, "summary": msg})
                continue
            results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": tu.id,
                    "content": json.dumps(
                        out.result, ensure_ascii=False, sort_keys=True, default=str
                    ),
                }
            )
            emit(
                "tool_result_summary",
                {"id": tu.id, "ok": True, "summary": out.summary, "refs": refs(out.result)},
            )
            if out.pending is not None:
                pending.append(out.pending.id)
                emit("pending_action", {"action": out.pending.model_dump()})
        return results, pending


__all__ = [
    "BETAS",
    "EFFORT",
    "MAX_TOKENS",
    "THINKING",
    "AgentLoop",
    "TurnResult",
    "Usage",
    "request_params",
]
