# SPDX-License-Identifier: Apache-2.0
"""Conversation transcripts: append-only rows in the ledger database (``messages`` table).

Assistant turns are stored exactly as the API returned their content blocks (thinking blocks
included, signatures intact). Nothing is ever edited or removed: preserved thinking requires an
append-only history, and N3 requires it anyway. :meth:`Transcript.api_messages` rebuilds the
request history; consecutive turns of the same role are joined (deterministically), which is
how the API itself reads them.
"""

from __future__ import annotations

import json
from typing import Any

from senseai_research.ledger.models import new_id, now
from senseai_research.ledger.store import LedgerError, Store

NOTE_PREFIX = "[Note from the research app, not typed by the user] "


class Transcript:
    def __init__(self, store: Store) -> None:
        self.store = store

    def create(self, user: str) -> str:
        cid = new_id("conv")
        self.store.append(
            "conversation.created",
            user,
            {"conversation": {"id": cid, "created_by": user, "created_at": now()}},
        )
        return cid

    def exists(self, conversation_id: str) -> bool:
        return self.store.get_raw("conversation", conversation_id) is not None

    def append(
        self,
        conversation_id: str,
        role: str,
        content: list[dict[str, Any]],
        meta: dict[str, Any] | None = None,
    ) -> int:
        if role not in ("user", "assistant"):
            raise LedgerError(f"bad role {role!r}")
        if not self.exists(conversation_id):
            raise LedgerError(f"there is no conversation {conversation_id!r}")
        with self.store.tx() as c:
            cur = c.execute(
                "INSERT INTO messages (conversation_id, at, role, content, meta) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    conversation_id,
                    now(),
                    role,
                    json.dumps(content, ensure_ascii=False),
                    json.dumps(meta or {}, ensure_ascii=False, sort_keys=True),
                ),
            )
            return int(cur.lastrowid or 0)

    def note(self, conversation_id: str, text: str, meta: dict[str, Any] | None = None) -> int:
        """A synthetic user message from the app (approvals, rejections), marked as such."""
        return self.append(
            conversation_id,
            "user",
            [{"type": "text", "text": NOTE_PREFIX + text}],
            {"synthetic": True, **(meta or {})},
        )

    def rows(self, conversation_id: str) -> list[dict[str, Any]]:
        out = []
        for n, at, role, content, meta in self.store._conn.execute(
            "SELECT n, at, role, content, meta FROM messages WHERE conversation_id=? ORDER BY n",
            (conversation_id,),
        ):
            out.append(
                {
                    "n": n,
                    "at": at,
                    "role": role,
                    "content": json.loads(content),
                    "meta": json.loads(meta),
                }
            )
        return out

    def api_messages(self, conversation_id: str) -> list[dict[str, Any]]:
        msgs: list[dict[str, Any]] = []
        for r in self.rows(conversation_id):
            if msgs and msgs[-1]["role"] == r["role"]:
                msgs[-1]["content"] = [*msgs[-1]["content"], *r["content"]]
            else:
                msgs.append({"role": r["role"], "content": list(r["content"])})
        return msgs


__all__ = ["NOTE_PREFIX", "Transcript"]
