# SPDX-License-Identifier: Apache-2.0
"""What the start screen lists (spec §10): open questions, and the least-verified values Panther
depends on. Plain records for the page and the CLI; computed from the ledger and the profiles."""

from __future__ import annotations

from typing import Any

from senseai_research.config import Settings
from senseai_research.ledger.models import Claim, Hypothesis
from senseai_research.ledger.store import Store

STATUS_RANK = {
    "unverified": 0,
    "disputed": 1,
    "extracted": 2,
    "superseded": 3,
    "rejected": 4,
    "verified": 5,
}
PRIORITY_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3}
CONFIDENCE_RANK = {"low": 0, "medium": 1, "high": 2, None: 1}


def _consumers(cfg: Settings) -> dict[str, list[str]]:
    if cfg.profiles_repo is None:
        return {}
    try:
        from senseai_profiles.research import consumers_by_path

        from senseai_research.impact.consumers import ProfilesTree, load

        _, specs = load(ProfilesTree(cfg.profiles_repo))
    except Exception:  # no profiles here: the list still works, without "read by"
        return {}
    return {p: sorted(c.label for c in cs) for p, cs in consumers_by_path(specs).items()}


def least_verified(
    store: Store, cfg: Settings, *, profile_prefix: str = "panther", limit: int = 20
) -> list[dict[str, Any]]:
    """Claims that a Panther profile reads, least verified first."""
    by_path = _consumers(cfg)
    rows = []
    for c in store.claims():
        read_by = [r for r in by_path.get(c.library_path, []) if r.startswith(profile_prefix)]
        if by_path and not read_by:
            continue
        rows.append((c, read_by))
    rows.sort(
        key=lambda x: (
            STATUS_RANK[x[0].status],
            CONFIDENCE_RANK.get(x[0].library_confidence, 1),
            -len(x[1]),
            x[0].library_path,
        )
    )
    return [
        {
            "claim_id": c.id,
            "library_path": c.library_path,
            "value": c.quantity.text(),
            "status": c.status,
            "why": c.unverified_reason or "",
            "species": c.species,
            "read_by": read_by,
        }
        for c, read_by in rows[:limit]
    ]


def open_questions(store: Store, *, limit: int = 30) -> list[dict[str, Any]]:
    hs = [h for h in store.all("hypothesis", Hypothesis) if h.status == "open"]
    hs.sort(key=lambda h: (PRIORITY_RANK.get(h.priority, 9), -len(h.consumers), h.id))
    return [
        {
            "hypothesis_id": h.id,
            "statement": h.statement,
            "priority": h.priority,
            "target": h.target,
            "read_by": h.consumers,
        }
        for h in hs[:limit]
    ]


def claim_card(store: Store, claim_id: str) -> dict[str, Any]:
    """The Claim tab: value, units, species, conditions, status and full revision history."""
    c = store.must("claim", claim_id, Claim)
    created = store.claim_created_event(c.id)
    history: list[dict[str, Any]] = []
    if created is not None:
        first = Claim.model_validate(created.payload["claim"])
        history.append(
            {
                "revision": 0,
                "at": created.at,
                "by": created.actor,
                "summary": f"Imported: {first.quantity.text()} ({first.status})",
                "reason": "",
                "changes": [],
            }
        )
    for r in store.claim_history(c.id):
        parts = []
        for ch in r.changes:
            if ch.field == "quantity":
                parts.append(f"value {_qtext(ch.old)} → {_qtext(ch.new)}")
            elif ch.field in ("status", "conditions", "species", "mapping", "method"):
                parts.append(f"{ch.field} {ch.old or '—'} → {ch.new or '—'}")
        history.append(
            {
                "revision": r.revision,
                "at": r.at,
                "by": r.by,
                "summary": "; ".join(parts) or "notes updated",
                "reason": r.reason,
                "changes": [ch.model_dump() for ch in r.changes],
            }
        )
    return {
        "claim": c.model_dump(),
        "value": c.quantity.text(),
        "history": history,
    }


def _qtext(q: Any) -> str:
    if not isinstance(q, dict):
        return str(q)
    if q.get("value") is not None:
        return f"{q['value']:g} {q.get('unit', '')}".strip()
    return f"{q.get('low'):g} to {q.get('high'):g} {q.get('unit', '')}".strip()


__all__ = ["claim_card", "least_verified", "open_questions"]
