# SPDX-License-Identifier: Apache-2.0
"""The ledger: SQLite (WAL) with an append-only ``events`` table and projected current state.

Every change is an :class:`~senseai_research.ledger.models.Event`. Projections (the
``entities`` table) are a fold over the events and can be rebuilt from them at any time
(:meth:`Store.rebuild`). Triggers make ``events``, ``source_texts`` and ``messages`` refuse
``UPDATE`` and ``DELETE``, so nothing is ever overwritten (N3).

The write rules that protect the library live here, not in the agent:

* an agent actor never changes a claim (it can only create pending actions);
* ``verified``, ``rejected`` and ``superseded`` need an Approval row by a person (N1);
* a claim at or above ``extracted`` needs a passage found in fetched text (N2).
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel

from senseai_research.ledger.models import (
    HUMAN_ONLY_STATUSES,
    NEEDS_PASSAGE,
    Approval,
    Claim,
    ClaimRevision,
    Event,
    Experiment,
    FieldChange,
    Hypothesis,
    Passage,
    Proposal,
    Source,
    now,
)

M = TypeVar("M", bound=BaseModel)


class LedgerError(ValueError):
    """A write the ledger refuses (and says why, in plain words)."""


MIGRATIONS: tuple[str, ...] = (
    # 1: events, projections, fetched texts
    """
    CREATE TABLE events (
        seq INTEGER PRIMARY KEY AUTOINCREMENT,
        at TEXT NOT NULL,
        actor TEXT NOT NULL,
        type TEXT NOT NULL,
        payload TEXT NOT NULL
    );
    CREATE TRIGGER events_no_update BEFORE UPDATE ON events
        BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;
    CREATE TRIGGER events_no_delete BEFORE DELETE ON events
        BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;
    CREATE TABLE entities (
        kind TEXT NOT NULL,
        id TEXT NOT NULL,
        data TEXT NOT NULL,
        seq INTEGER NOT NULL,
        PRIMARY KEY (kind, id)
    );
    CREATE TABLE source_texts (
        sha256 TEXT PRIMARY KEY,
        text TEXT NOT NULL,
        sections TEXT NOT NULL DEFAULT '[]'
    );
    CREATE TRIGGER source_texts_no_update BEFORE UPDATE ON source_texts
        BEGIN SELECT RAISE(ABORT, 'fetched texts are immutable'); END;
    CREATE TRIGGER source_texts_no_delete BEFORE DELETE ON source_texts
        BEGIN SELECT RAISE(ABORT, 'fetched texts are immutable'); END;
    """,
    # 2: conversation transcripts (append-only; agent/transcript.py)
    """
    CREATE TABLE messages (
        n INTEGER PRIMARY KEY AUTOINCREMENT,
        conversation_id TEXT NOT NULL,
        at TEXT NOT NULL,
        role TEXT NOT NULL,
        content TEXT NOT NULL,
        meta TEXT NOT NULL DEFAULT '{}'
    );
    CREATE INDEX messages_by_conv ON messages (conversation_id, n);
    CREATE TRIGGER messages_no_update BEFORE UPDATE ON messages
        BEGIN SELECT RAISE(ABORT, 'transcripts are append-only'); END;
    CREATE TRIGGER messages_no_delete BEFORE DELETE ON messages
        BEGIN SELECT RAISE(ABORT, 'transcripts are append-only'); END;
    """,
)

#: event type -> (entity kind, payload key holding the full record)
_CREATE_EVENTS: Mapping[str, tuple[str, str]] = {
    "source.added": ("source", "source"),
    "source.updated": ("source", "source"),
    "passage.added": ("passage", "passage"),
    "claim.created": ("claim", "claim"),
    "hypothesis.created": ("hypothesis", "hypothesis"),
    "hypothesis.updated": ("hypothesis", "hypothesis"),
    "experiment.created": ("experiment", "experiment"),
    "experiment.updated": ("experiment", "experiment"),
    "impact.recorded": ("impact", "impact"),
    "proposal.created": ("proposal", "proposal"),
    "proposal.updated": ("proposal", "proposal"),
    "approval.recorded": ("approval", "approval"),
    "action.pending": ("action", "action"),
    "action.resolved": ("action", "action"),
    "conversation.created": ("conversation", "conversation"),
    "handoff.released": ("handoff", "handoff"),
}


def _dumps(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


class Store:
    """One ledger database. Thread-safe for the server's purposes (one writer lock)."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._migrate()

    def close(self) -> None:
        self._conn.close()

    # -- schema ------------------------------------------------------------------

    def _migrate(self) -> None:
        with self._lock:
            (version,) = self._conn.execute("PRAGMA user_version").fetchone()
            for i, sql in enumerate(MIGRATIONS[version:], start=version + 1):
                self._conn.executescript(f"BEGIN;\n{sql}\nPRAGMA user_version={i};\nCOMMIT;")

    @property
    def schema_version(self) -> int:
        return int(self.query("PRAGMA user_version")[0][0])

    def query(self, sql: str, args: Any = ()) -> list[Any]:
        """Run a read under the store's lock. The connection is shared by the server's threads,
        so every use of it, read or write, is serialised here or in :meth:`tx`."""
        with self._lock:
            return self._conn.execute(sql, args).fetchall()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """One write transaction; nested calls join the outer one."""
        with self._lock:
            if self._conn.in_transaction:
                yield self._conn
                return
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            self._conn.execute("COMMIT")

    # -- the log -----------------------------------------------------------------

    def append(self, type_: str, actor: str, payload: Mapping[str, Any]) -> Event:
        """Append one event and update the projections, atomically."""
        at = now()
        body = json.loads(_dumps(payload))
        with self.tx() as c:
            cur = c.execute(
                "INSERT INTO events (at, actor, type, payload) VALUES (?, ?, ?, ?)",
                (at, actor, type_, _dumps(body)),
            )
            seq = int(cur.lastrowid or 0)
            ev = Event(seq=seq, at=at, actor=actor, type=type_, payload=body)
            self._apply(c, ev)
        return ev

    def events(
        self, *, type_prefix: str | None = None, since: int = 0, entity_id: str | None = None
    ) -> list[Event]:
        sql = "SELECT seq, at, actor, type, payload FROM events WHERE seq > ?"
        args: list[Any] = [since]
        if type_prefix:
            sql += " AND type LIKE ?"
            args.append(type_prefix + "%")
        if entity_id:
            sql += " AND payload LIKE ?"
            args.append(f"%{json.dumps(entity_id)}%")
        sql += " ORDER BY seq"
        out = []
        for seq, at, actor, t, payload in self.query(sql, args):
            out.append(Event(seq=seq, at=at, actor=actor, type=t, payload=json.loads(payload)))
        return out

    def _apply(self, c: sqlite3.Connection, ev: Event) -> None:
        if ev.type in _CREATE_EVENTS:
            kind, key = _CREATE_EVENTS[ev.type]
            rec = ev.payload[key]
            c.execute(
                "INSERT OR REPLACE INTO entities (kind, id, data, seq) VALUES (?, ?, ?, ?)",
                (kind, rec["id"], _dumps(rec), ev.seq),
            )
        elif ev.type == "claim.revised":
            row = c.execute(
                "SELECT data FROM entities WHERE kind='claim' AND id=?", (ev.payload["claim_id"],)
            ).fetchone()
            if row is None:
                raise LedgerError(f"no claim {ev.payload['claim_id']!r}")
            data = json.loads(row[0])
            for ch in ev.payload["changes"]:
                data[ch["field"]] = ch["new"]
            data["revision"] = ev.payload["revision"]
            c.execute(
                "UPDATE entities SET data=?, seq=? WHERE kind='claim' AND id=?",
                (_dumps(data), ev.seq, data["id"]),
            )
        # other event types (notes, job progress) carry no projection

    def rebuild(self) -> None:
        """Drop the projections and replay every event. The log is the source of truth."""
        with self.tx() as c:
            c.execute("DELETE FROM entities")
            for ev in self.events():
                self._apply(c, ev)

    def snapshot(self) -> dict[str, dict[str, Any]]:
        """All projected entities as ``{kind/id: data}`` (for tests and export)."""
        return {
            f"{k}/{i}": json.loads(d)
            for k, i, d in self.query("SELECT kind, id, data FROM entities ORDER BY 1, 2")
        }

    # -- generic reads -----------------------------------------------------------

    def get_raw(self, kind: str, id_: str) -> dict[str, Any] | None:
        rows = self.query("SELECT data FROM entities WHERE kind=? AND id=?", (kind, id_))
        return dict(json.loads(rows[0][0])) if rows else None

    def list_raw(self, kind: str) -> list[dict[str, Any]]:
        return [
            json.loads(d)
            for (d,) in self.query("SELECT data FROM entities WHERE kind=? ORDER BY id", (kind,))
        ]

    def get(self, kind: str, id_: str, model: type[M]) -> M | None:
        raw = self.get_raw(kind, id_)
        return None if raw is None else model.model_validate(raw)

    def must(self, kind: str, id_: str, model: type[M]) -> M:
        got = self.get(kind, id_, model)
        if got is None:
            raise LedgerError(f"there is no {kind} with id {id_!r}")
        return got

    def all(self, kind: str, model: type[M]) -> list[M]:
        return [model.model_validate(r) for r in self.list_raw(kind)]

    # -- sources and texts -------------------------------------------------------

    def store_text(self, text: str, sections: list[tuple[str, int]] | None = None) -> str:
        sha = hashlib.sha256(text.encode()).hexdigest()
        with self.tx() as c:
            c.execute(
                "INSERT OR IGNORE INTO source_texts (sha256, text, sections) VALUES (?, ?, ?)",
                (sha, text, _dumps([list(s) for s in sections or []])),
            )
        return sha

    def text(self, sha256: str) -> tuple[str, list[tuple[str, int]]] | None:
        rows = self.query("SELECT text, sections FROM source_texts WHERE sha256=?", (sha256,))
        if not rows:
            return None
        row = rows[0]
        return row[0], [(str(a), int(b)) for a, b in json.loads(row[1])]

    def put_source(self, src: Source, actor: str) -> Source:
        existing = self.get_raw("source", src.id)
        if existing is None:
            self.append("source.added", actor, {"source": src.model_dump()})
        elif existing != json.loads(_dumps(src.model_dump())):
            self.append("source.updated", actor, {"source": src.model_dump()})
        return src

    def add_passage(self, p: Passage, actor: str) -> Passage:
        """Record a passage. Its text must be a verbatim slice of the stored fetched text (N2)."""
        src = self.must("source", p.source_id, Source)
        stored = self.text(src.fetched_text_sha256 or "")
        if stored is None:
            raise LedgerError(f"source {src.id} has no fetched text; a passage needs one")
        if stored[0][p.char_start : p.char_end] != p.text:
            raise LedgerError("passage text is not at the given offsets of the fetched text")
        if self.get_raw("passage", p.id) is None:
            self.append("passage.added", actor, {"passage": p.model_dump()})
        return p

    # -- claims ------------------------------------------------------------------

    def create_claim(self, claim: Claim, actor: str) -> Claim:
        if actor.startswith("agent:"):
            raise LedgerError("the agent cannot create claims; it can only propose them (N1)")
        if claim.status in HUMAN_ONLY_STATUSES:
            raise LedgerError(f"a new claim cannot start as {claim.status!r}")
        if claim.passage_id is not None:
            self.must("passage", claim.passage_id, Passage)
        if self.get_raw("claim", claim.id) is not None:
            raise LedgerError(f"claim {claim.id} already exists")
        self.append("claim.created", actor, {"claim": claim.model_dump()})
        return claim

    def revise_claim(
        self,
        claim_id: str,
        changes: Mapping[str, Any],
        *,
        reason: str,
        by: str,
        approval_id: str | None = None,
    ) -> Claim:
        """Make a new revision of a claim. The old values stay in the event log (N3)."""
        if by.startswith("agent:"):
            raise LedgerError("the agent cannot change a claim; it can only propose a change (N1)")
        if not reason.strip():
            raise LedgerError("a revision needs a reason")
        claim = self.must("claim", claim_id, Claim)
        new_status = changes.get("status", claim.status)
        if new_status != claim.status and new_status in HUMAN_ONLY_STATUSES:
            self._require_approval(approval_id, by)
        if "id" in changes or "revision" in changes:
            raise LedgerError("id and revision cannot be changed")
        current = claim.model_dump()
        diffs = [
            FieldChange(field=k, old=current.get(k), new=v)
            for k, v in sorted(changes.items())
            if current.get(k) != v
        ]
        if not diffs:
            return claim
        merged = Claim.model_validate({**current, **{d.field: d.new for d in diffs}})
        if merged.status in NEEDS_PASSAGE and merged.passage_id:
            self.must("passage", merged.passage_id, Passage)
        rev = ClaimRevision(
            claim_id=claim_id,
            revision=claim.revision + 1,
            changes=diffs,
            reason=reason.strip(),
            by=by,
            at=now(),
            approval_id=approval_id,
        )
        self.append("claim.revised", by, rev.model_dump())
        return self.must("claim", claim_id, Claim)

    def _require_approval(self, approval_id: str | None, by: str) -> Approval:
        if not by.startswith("user:"):
            raise LedgerError("only a person can verify, reject or supersede a claim (N1)")
        if approval_id is None:
            raise LedgerError("verifying, rejecting or superseding a claim needs an Approval (N1)")
        ap = self.must("approval", approval_id, Approval)
        if ap.decision != "approve":
            raise LedgerError("that Approval was a rejection")
        return ap

    def claim_history(self, claim_id: str) -> list[ClaimRevision]:
        out: list[ClaimRevision] = []
        for ev in self.events(type_prefix="claim.revised", entity_id=claim_id):
            if ev.payload.get("claim_id") == claim_id:
                out.append(ClaimRevision.model_validate(ev.payload))
        return out

    def claim_created_event(self, claim_id: str) -> Event | None:
        for ev in self.events(type_prefix="claim.created", entity_id=claim_id):
            if ev.payload["claim"]["id"] == claim_id:
                return ev
        return None

    def claims(self) -> list[Claim]:
        return self.all("claim", Claim)

    # -- other records -----------------------------------------------------------

    def put_hypothesis(self, h: Hypothesis, actor: str) -> Hypothesis:
        kind = "hypothesis.created" if self.get_raw("hypothesis", h.id) is None else None
        if kind is None:
            if self.get_raw("hypothesis", h.id) == json.loads(_dumps(h.model_dump())):
                return h
            kind = "hypothesis.updated"
        self.append(kind, actor, {"hypothesis": h.model_dump()})
        return h

    def put_experiment(self, e: Experiment, actor: str) -> Experiment:
        kind = "experiment.created" if self.get_raw("experiment", e.id) is None else None
        self.append(kind or "experiment.updated", actor, {"experiment": e.model_dump()})
        return e

    def record_approval(self, ap: Approval) -> Approval:
        if self.get_raw("approval", ap.id) is not None:
            raise LedgerError("approvals are recorded once")
        self.append("approval.recorded", ap.by, {"approval": ap.model_dump()})
        return ap

    def put_proposal(self, p: Proposal, actor: str) -> Proposal:
        """Create or update a library-change proposal. Only verified-quote claims qualify (N2)."""
        if self.get_raw("proposal", p.id) is None:
            for cid in p.claim_ids:
                c = self.must("claim", cid, Claim)
                if c.status == "unverified" or not c.passage_id:
                    raise LedgerError(
                        f"claim {cid} has no verified quote (status {c.status}); "
                        "it cannot be proposed to the library (N2)"
                    )
                if c.status in ("rejected", "superseded"):
                    raise LedgerError(f"claim {cid} is {c.status}; it cannot be proposed")
            self.append("proposal.created", actor, {"proposal": p.model_dump()})
        else:
            self.append("proposal.updated", actor, {"proposal": p.model_dump()})
        return p


__all__ = ["MIGRATIONS", "LedgerError", "Store"]
