# SPDX-License-Identifier: Apache-2.0
"""Pending actions: the only way an agent's write reaches the ledger or the library (N1).

An agent write tool calls one of the ``propose_*`` functions here. They check the request
(quotes must be found in fetched text before anything is shown to a person, N2), store a
:class:`PendingAction` with a plain-language card, and change nothing else. :func:`approve`
and :func:`reject` are called by the server when a person clicks a button, with that person's
name and rationale. Only then is the change applied, as that person, citing the Approval row.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from senseai_research import literature
from senseai_research.config import Settings
from senseai_research.ledger.models import (
    Approval,
    Claim,
    Experiment,
    Hypothesis,
    Passage,
    PendingAction,
    Quantity,
    Source,
    new_id,
)
from senseai_research.ledger.store import LedgerError, Store
from senseai_research.literature.passages import locate

#: Statuses an agent may ask for. Verified / rejected / superseded are set by people (N1).
AGENT_STATUSES = ("disputed", "extracted")
CLAIM_FIELDS = (
    "value",
    "low",
    "high",
    "unit",
    "conditions",
    "species",
    "method",
    "mapping",
    "status",
    "source_id",
    "quote",
)


class ActionError(ValueError):
    """The request cannot become an action (said in plain words, returned to the agent)."""


# --- checks ------------------------------------------------------------------------------


def verify_quote(store: Store, source_id: str, quote: str) -> tuple[Source, Any]:
    """The quote must be in the source's fetched text (N2). Returns the source and location."""
    src = store.get("source", source_id, Source)
    if src is None:
        raise ActionError(f"there is no source {source_id!r}; fetch it first")
    if not src.fetched_text_sha256:
        raise ActionError(f"{source_id} has no fetched text; fetch the full text first")
    stored = store.text(src.fetched_text_sha256)
    if stored is None:
        raise ActionError(f"the fetched text of {source_id} is missing")
    loc = locate(stored[0], quote, stored[1])
    if loc is None:
        raise ActionError(
            "that quote is not in the fetched text, word for word. Copy the sentence exactly."
        )
    return src, loc


def _numbers_in_quote(values: Sequence[float | None], quote: str) -> None:
    for v in values:
        if v is not None and not literature.value_in_quote(float(v), quote):
            raise ActionError(
                f"the number {v:g} does not appear in the quote; a value needs a quote that "
                "states it (N2)"
            )


def _quantity(cur: Quantity | None, ch: Mapping[str, Any]) -> Quantity | None:
    if ch.get("value") is None and ch.get("low") is None and ch.get("high") is None:
        return None
    unit = ch.get("unit") or (cur.unit if cur else None)
    if not unit:
        raise ActionError("a new value needs its unit")
    if cur is not None and unit != cur.unit:
        raise ActionError(
            f"the library keeps this value in {cur.unit!r}; give it in that unit (got {unit!r})"
        )
    if ch.get("value") is not None:
        return Quantity(value=float(ch["value"]), unit=unit)
    if ch.get("low") is None or ch.get("high") is None:
        raise ActionError("a range needs both low and high")
    return Quantity(low=float(ch["low"]), high=float(ch["high"]), unit=unit)


def _store_action(store: Store, a: PendingAction) -> PendingAction:
    store.append("action.pending", a.created_by, {"action": a.model_dump()})
    return a


# --- the four kinds ----------------------------------------------------------------------


def propose_claim_revision(
    store: Store,
    *,
    claim_id: str,
    changes: Mapping[str, Any],
    reason: str,
    actor: str,
    conversation_id: str | None = None,
    tool_use_id: str | None = None,
) -> PendingAction:
    claim = store.get("claim", claim_id, Claim)
    if claim is None:
        raise ActionError(f"there is no claim {claim_id!r}")
    ch = {k: v for k, v in changes.items() if k in CLAIM_FIELDS and v not in (None, "")}
    if not ch:
        raise ActionError("nothing to change")
    if not reason.strip():
        raise ActionError("a revision needs a reason")
    status = ch.get("status")
    if status is not None and status not in AGENT_STATUSES:
        raise ActionError(
            f"you can propose {' or '.join(AGENT_STATUSES)}; verified, rejected and superseded "
            "are set by the reviewer"
        )
    q = _quantity(claim.quantity, ch)
    rows: list[dict[str, Any]] = []
    passage_text = None
    if "quote" in ch or "source_id" in ch:
        if not ch.get("quote") or not ch.get("source_id"):
            raise ActionError("a quote needs its source_id, and a source_id needs a quote")
        _, loc = verify_quote(store, ch["source_id"], ch["quote"])
        passage_text = loc.text
        if q is not None:
            _numbers_in_quote([q.value, q.low, q.high], loc.text)
    elif q is not None:
        raise ActionError("a new value needs the quote that states it (source_id and quote)")
    if status == "extracted" and passage_text is None and not claim.passage_id:
        raise ActionError("'extracted' needs a verified quote (source_id and quote)")
    if q is not None:
        rows.append({"label": "Value", "before": claim.quantity.text(), "after": q.text()})
    for f, label in (
        ("conditions", "Conditions"),
        ("species", "Species"),
        ("method", "Method"),
        ("mapping", "Matches the library field"),
        ("status", "Status"),
    ):
        if f in ch:
            rows.append({"label": label, "before": getattr(claim, f) or "—", "after": ch[f]})
    card = {
        "title": f"Change {claim.library_path}",
        "summary": reason.strip(),
        "rows": rows,
        "quote": passage_text,
        "source_id": ch.get("source_id"),
        "note": "Nothing changes until you approve. The current value stays in the history.",
    }
    return _store_action(
        store,
        PendingAction(
            id=new_id("act"),
            created_by=actor,
            kind="claim_revision",
            conversation_id=conversation_id,
            tool_use_id=tool_use_id,
            input={"claim_id": claim_id, "changes": ch, "reason": reason.strip()},
            card=card,
        ),
    )


def propose_new_claim(
    store: Store,
    *,
    library_path: str,
    quantity: Mapping[str, Any],
    source_id: str,
    quote: str,
    conditions: str | None,
    species: str | None,
    method: str | None,
    mapping: str,
    actor: str,
    conversation_id: str | None = None,
    tool_use_id: str | None = None,
) -> PendingAction:
    existing = [c for c in store.claims() if c.library_path == library_path]
    cur = existing[0].quantity if existing else None
    q = _quantity(cur, quantity)
    if q is None:
        raise ActionError("a new claim needs a value, or a low and a high")
    _, loc = verify_quote(store, source_id, quote)
    _numbers_in_quote([q.value, q.low, q.high], loc.text)
    card = {
        "title": f"New evidence for {library_path}",
        "summary": f"{q.text()} from {source_id}",
        "rows": [
            {"label": "Value", "before": cur.text() if cur else "—", "after": q.text()},
            {"label": "Conditions", "before": "", "after": conditions or "—"},
            {"label": "Species", "before": "", "after": species or "—"},
            {"label": "Matches the library field", "before": "", "after": mapping},
        ],
        "quote": loc.text,
        "source_id": source_id,
        "note": "This adds a claim to the ledger. The library itself does not change.",
    }
    return _store_action(
        store,
        PendingAction(
            id=new_id("act"),
            created_by=actor,
            kind="new_claim",
            conversation_id=conversation_id,
            tool_use_id=tool_use_id,
            input={
                "library_path": library_path,
                "quantity": q.model_dump(),
                "source_id": source_id,
                "quote": quote,
                "conditions": conditions,
                "species": species,
                "method": method,
                "mapping": mapping,
            },
            card=card,
        ),
    )


def propose_hypothesis(
    store: Store,
    *,
    statement: str,
    prediction: str,
    linked_claims: Sequence[str],
    actor: str,
    conversation_id: str | None = None,
    tool_use_id: str | None = None,
) -> PendingAction:
    for cid in linked_claims:
        if store.get_raw("claim", cid) is None:
            raise ActionError(f"there is no claim {cid!r}")
    if not prediction.strip():
        raise ActionError("state the prediction (what result would support or refute it) first")
    return _store_action(
        store,
        PendingAction(
            id=new_id("act"),
            created_by=actor,
            kind="hypothesis",
            conversation_id=conversation_id,
            tool_use_id=tool_use_id,
            input={
                "statement": statement,
                "prediction": prediction,
                "linked_claims": list(linked_claims),
            },
            card={
                "title": "New hypothesis",
                "summary": statement,
                "rows": [{"label": "Prediction", "before": "", "after": prediction}],
                "note": "This records the hypothesis. It changes no value.",
            },
        ),
    )


def propose_library_change(
    store: Store,
    cfg: Settings,
    *,
    claim_ids: Sequence[str],
    experiment_ids: Sequence[str],
    rationale: str,
    actor: str,
    conversation_id: str | None = None,
    tool_use_id: str | None = None,
) -> PendingAction:
    from senseai_research.proposals.diff import build_patch

    claims = [store.must("claim", c, Claim) for c in claim_ids]
    for c in claims:
        if c.status == "unverified" or not c.passage_id:
            raise ActionError(
                f"{c.library_path} has no verified quote yet, so it cannot go to the library (N2)"
            )
        if c.status in ("rejected", "superseded"):
            raise ActionError(f"{c.id} is {c.status}")
    for e in experiment_ids:
        store.must("experiment", e, Experiment)
    if cfg.profiles_repo is None:
        raise ActionError("SENSEAI_PROFILES_REPO is not set")
    patch = build_patch(store, cfg.profiles_repo, claims)
    return _store_action(
        store,
        PendingAction(
            id=new_id("act"),
            created_by=actor,
            kind="proposal",
            conversation_id=conversation_id,
            tool_use_id=tool_use_id,
            input={
                "claim_ids": list(claim_ids),
                "experiment_ids": list(experiment_ids),
                "rationale": rationale.strip(),
            },
            card={
                "title": patch.title,
                "summary": rationale.strip(),
                "rows": patch.rows,
                "diff": patch.diff,
                "note": (
                    "Approving opens a pull request on profiles. The library changes only "
                    "when that PR is reviewed and merged."
                ),
            },
        ),
    )


def propose_handoff(
    store: Store,
    cfg: Settings,
    *,
    clade: str,
    min_readiness: str = "provisional",
    actor: str,
    conversation_id: str | None = None,
    tool_use_id: str | None = None,
) -> PendingAction:
    """Ask a person to release the clade's handoff package to circuits (docs/HANDOFF.md)."""
    from senseai_research.handoff import HandoffError, Policy, build

    try:
        h = build(store, cfg, clade, policy=Policy.model_validate({"min_readiness": min_readiness}))
    except (HandoffError, ValueError) as e:
        raise ActionError(str(e)) from e
    rows = [
        {"label": k.knob, "before": f"{k.default} (default)", "after": str(h.circuits[k.knob])}
        for k in h.knobs
        if k.knob in h.circuits
    ]
    return _store_action(
        store,
        PendingAction(
            id=new_id("act"),
            created_by=actor,
            kind="handoff",
            conversation_id=conversation_id,
            tool_use_id=tool_use_id,
            input={"clade": h.clade, "min_readiness": min_readiness, "digest": h.digest},
            card={
                "title": f"Release the {h.clade} evidence to circuits",
                "summary": " ".join(h.summary),
                "rows": rows,
                "note": (
                    "Approving writes the package for circuits"
                    + (" and opens a pull request on circuits" if cfg.circuits_repo else "")
                    + ". Nothing else changes; circuits builds the model from it."
                ),
            },
        ),
    )


# --- approval ----------------------------------------------------------------------------


@dataclass
class Outcome:
    action: PendingAction
    approval: Approval
    message: str  # what is posted to the conversation, in plain words


def _pending(store: Store, action_id: str) -> PendingAction:
    a = store.get("action", action_id, PendingAction)
    if a is None:
        raise ActionError(f"there is no pending action {action_id!r}")
    if a.status != "pending":
        raise ActionError(f"that action was already {a.status}")
    return a


def _record(
    store: Store, a: PendingAction, decision: Literal["approve", "reject"], by: str, rationale: str
) -> Approval:
    ap = Approval(
        id=new_id("apr"),
        created_by=by,
        target_kind=a.kind,
        target_id=a.id,
        decision=decision,
        by=by,
        rationale=rationale,
    )
    return store.record_approval(ap)


def reject(store: Store, action_id: str, *, by: str, rationale: str) -> Outcome:
    a = _pending(store, action_id)
    ap = _record(store, a, "reject", by, rationale)
    done = a.model_copy(update={"status": "rejected", "approval_id": ap.id})
    store.append("action.resolved", by, {"action": done.model_dump()})
    who = by.removeprefix("user:")
    return Outcome(
        done,
        ap,
        f"{who} rejected “{a.card.get('title', a.kind)}”. Their reason: {ap.rationale} "
        "Nothing was changed.",
    )


def approve(store: Store, cfg: Settings, action_id: str, *, by: str, rationale: str) -> Outcome:
    """Record the Approval, then apply the action as the approving person."""
    a = _pending(store, action_id)
    ap = _record(store, a, "approve", by, rationale)
    who = by.removeprefix("user:")
    try:
        result, message = _apply(store, cfg, a, ap)
    except (ActionError, LedgerError, ValueError, OSError) as e:
        failed = a.model_copy(update={"status": "failed", "approval_id": ap.id, "error": str(e)})
        store.append("action.resolved", by, {"action": failed.model_dump()})
        return Outcome(
            failed, ap, f"{who} approved “{a.card.get('title')}”, but it could not be applied: {e}"
        )
    done = a.model_copy(update={"status": "approved", "approval_id": ap.id, "result": result})
    store.append("action.resolved", by, {"action": done.model_dump()})
    return Outcome(
        done,
        ap,
        f"{who} approved “{a.card.get('title')}”. Their reason: {ap.rationale} {message}",
    )


def _passage(store: Store, source_id: str, quote: str, by: str) -> Passage:
    src, loc = verify_quote(store, source_id, quote)
    p = Passage(
        id=new_id("psg"),
        created_by=by,
        source_id=src.id,
        char_start=loc.char_start,
        char_end=loc.char_end,
        text=loc.text,
        section=loc.section,
        context_before=loc.context_before,
        context_after=loc.context_after,
    )
    return store.add_passage(p, by)


def _apply(
    store: Store, cfg: Settings, a: PendingAction, ap: Approval
) -> tuple[dict[str, Any], str]:
    by = ap.by
    if a.kind == "claim_revision":
        claim = store.must("claim", a.input["claim_id"], Claim)
        ch = dict(a.input["changes"])
        upd: dict[str, Any] = {"provenance": "agent-proposed"}
        q = _quantity(claim.quantity, ch)
        if q is not None:
            upd["quantity"] = q.model_dump()
        for f in ("conditions", "species", "method", "mapping", "status"):
            if f in ch:
                upd[f] = ch[f]
        if ch.get("quote"):
            p = _passage(store, ch["source_id"], ch["quote"], by)
            upd["passage_id"] = p.id
            if p.source_id not in claim.source_ids:
                upd["source_ids"] = [*claim.source_ids, p.source_id]
            upd["unverified_reason"] = None
        note = f"{ap.rationale} ({by.removeprefix('user:')}, {ap.at[:10]})"
        upd["notes_by_reviewer"] = (
            f"{claim.notes_by_reviewer}\n{note}" if claim.notes_by_reviewer else note
        )
        new = store.revise_claim(
            claim.id,
            upd,
            reason=f"{a.input['reason']} Approved: {ap.rationale}",
            by=by,
            approval_id=ap.id,
        )
        return (
            {"claim_id": new.id, "revision": new.revision},
            f"Claim {new.library_path} is now revision {new.revision} "
            f"({new.quantity.text()}, {new.status}). The previous value stays in its history.",
        )
    if a.kind == "new_claim":
        i = a.input
        p = _passage(store, i["source_id"], i["quote"], by)
        c = Claim(
            id=new_id("clm"),
            created_by=by,
            library_path=i["library_path"],
            quantity=Quantity.model_validate(i["quantity"]),
            species=i.get("species"),
            conditions=i.get("conditions"),
            method=i.get("method"),
            source_ids=[p.source_id],
            passage_id=p.id,
            mapping=i["mapping"],
            status="extracted",
            provenance="agent-proposed",
            notes_by_reviewer=ap.rationale,
        )
        store.create_claim(c, by)
        return {"claim_id": c.id}, f"New claim {c.id} ({c.quantity.text()}) was added."
    if a.kind == "hypothesis":
        i = a.input
        h = Hypothesis(
            id=new_id("hyp"),
            created_by=by,
            statement=i["statement"],
            prediction=i["prediction"],
            linked_claims=list(i["linked_claims"]),
            origin=f"conversation:{a.conversation_id}" if a.conversation_id else "user",
        )
        store.put_hypothesis(h, by)
        return {"hypothesis_id": h.id}, f"Hypothesis {h.id} was recorded as open."
    if a.kind == "proposal":
        from senseai_research.proposals import submit

        prop = submit(store, cfg, a, ap)
        where = (
            f"Pull request: {prop.pr_url}"
            if prop.pr_url
            else "No GITHUB_TOKEN is set, so the patch and the PR text were saved for download "
            "instead of opening a PR."
        )
        return {"proposal_id": prop.id, "pr_url": prop.pr_url}, where
    if a.kind == "handoff":
        from senseai_research.handoff import Policy, build
        from senseai_research.handoff.deliver import release

        pkg = build(
            store,
            cfg,
            a.input["clade"],
            policy=Policy.model_validate({"min_readiness": a.input["min_readiness"]}),
        )
        if pkg.digest != a.input["digest"]:
            raise ActionError(
                "the evidence changed since this release was proposed; propose it again so "
                "you approve what will be sent"
            )
        rel = release(store, cfg, a, ap, pkg)
        where = f"Pull request: {rel.pr_url}" if rel.pr_url else f"Saved as {rel.file}."
        if rel.pr_error:
            where += f" The pull request on circuits could not be opened: {rel.pr_error}"
        return (
            {"handoff_id": rel.id, "digest": rel.digest, "pr_url": rel.pr_url},
            f"The {pkg.clade} package ({len(pkg.circuits)} knob values) was released. {where}",
        )
    raise ActionError(f"unknown action kind {a.kind!r}")


def pending(store: Store, conversation_id: str | None = None) -> list[PendingAction]:
    return [
        a
        for a in store.all("action", PendingAction)
        if a.status == "pending"
        and (conversation_id is None or a.conversation_id == conversation_id)
    ]


__all__ = [
    "AGENT_STATUSES",
    "ActionError",
    "Outcome",
    "approve",
    "pending",
    "propose_claim_revision",
    "propose_hypothesis",
    "propose_library_change",
    "propose_new_claim",
    "reject",
    "verify_quote",
]
