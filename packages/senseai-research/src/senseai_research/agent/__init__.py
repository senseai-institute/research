# SPDX-License-Identifier: Apache-2.0
"""The agent: conversations, turns, and the approval round-trip.

:class:`Agent` is what the server and the ``ask`` command use. :meth:`Agent.decide` records a
person's Approve or Reject (with rationale), applies it, and posts a synthetic note to the
conversation so the agent knows what happened (spec §8).
"""

from __future__ import annotations

from typing import Any

from senseai_research import actions
from senseai_research.agent.loop import AgentLoop, Emit, TurnResult
from senseai_research.agent.tools import BY_NAME
from senseai_research.agent.transcript import NOTE_PREFIX, Transcript
from senseai_research.config import Settings
from senseai_research.experiments.jobs import ExperimentQueue
from senseai_research.ledger.models import PendingAction
from senseai_research.ledger.store import Store


class Agent:
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
        self.loop = AgentLoop(store, cfg, queue=queue, extract_call=extract_call, offline=offline)
        self.transcript = Transcript(store)

    def new_conversation(self, user: str) -> str:
        return self.transcript.create(user)

    def send(
        self,
        conversation_id: str,
        text: str,
        *,
        emit: Emit,
        focus: dict[str, Any] | None = None,
        user: str = "user:senseai",
    ) -> TurnResult:
        return self.loop.run_turn(conversation_id, text, emit=emit, focus=focus, user=user)

    def decide(
        self, action_id: str, decision: str, *, user: str, rationale: str
    ) -> actions.Outcome:
        """A person clicked Approve or Reject. Only this path applies an agent's proposal (N1)."""
        if decision == "approve":
            out = actions.approve(self.store, self.cfg, action_id, by=user, rationale=rationale)
        elif decision == "reject":
            out = actions.reject(self.store, action_id, by=user, rationale=rationale)
        else:
            raise actions.ActionError("decision must be approve or reject")
        if out.action.conversation_id and self.transcript.exists(out.action.conversation_id):
            self.transcript.note(
                out.action.conversation_id,
                out.message,
                {"action_id": action_id, "decision": decision, "approval_id": out.approval.id},
            )
        return out

    def view(self, conversation_id: str) -> list[dict[str, Any]]:
        """The transcript as the page shows it: plain text, tool chips and cards, no raw JSON."""
        names: dict[str, str] = {}
        out: list[dict[str, Any]] = []
        for row in self.transcript.rows(conversation_id):
            for b in row["content"]:
                t = b.get("type")
                if t == "text":
                    text = b["text"]
                    if row["meta"].get("synthetic"):
                        out.append({"kind": "note", "text": text.removeprefix(NOTE_PREFIX)})
                    elif row["role"] == "user":
                        if not text.startswith("(The person has"):
                            out.append({"kind": "user", "text": text})
                    else:
                        out.append({"kind": "assistant", "text": text})
                elif t == "thinking" and b.get("thinking"):
                    out.append({"kind": "thinking", "text": b["thinking"]})
                elif t == "tool_use":
                    names[b["id"]] = b["name"]
                    spec = BY_NAME.get(b["name"])
                    label = spec.label(b.get("input") or {}) if spec else b["name"]
                    out.append({"kind": "tool", "id": b["id"], "name": b["name"], "label": label})
                elif t == "tool_result":
                    out.append(
                        {
                            "kind": "tool_result",
                            "id": b["tool_use_id"],
                            "ok": not b.get("is_error", False),
                        }
                    )
        for a in self.store.all("action", PendingAction):
            if a.conversation_id == conversation_id:
                out.append({"kind": "card", "action": a.model_dump()})
        return out


__all__ = ["Agent"]
