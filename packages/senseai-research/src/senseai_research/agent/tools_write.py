# SPDX-License-Identifier: Apache-2.0
"""Write tools: they never execute a change. Each creates a pending action and the loop ends the
turn with a card a person must approve or reject (N1).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from senseai_research import actions
from senseai_research.agent.toolkit import (
    _Q,
    ToolContext,
    ToolError,
    ToolOutput,
    ToolSpec,
    _enum_n,
    _ns,
    _quantity_arg,
    _s,
    _strs,
)
from senseai_research.ledger.models import PendingAction
from senseai_research.ledger.store import Store


def _pending(
    ctx: ToolContext, fn: Callable[..., PendingAction], tool_use_id: str, **kw: Any
) -> ToolOutput:
    try:
        act = fn(
            ctx.store,
            actor=ctx.actor,
            conversation_id=ctx.conversation_id,
            tool_use_id=tool_use_id,
            **kw,
        )
    except actions.ActionError as e:
        raise ToolError(str(e)) from e
    res = {
        "pending_action_id": act.id,
        "status": "waiting for the person to approve or reject",
        "card": act.card.get("title"),
        "nothing_has_changed_yet": True,
    }
    return ToolOutput(res, f"Proposed: {act.card.get('title')}", pending=act)


def propose_claim_revision(ctx: ToolContext, a: dict[str, Any]) -> ToolOutput:
    return _pending(
        ctx,
        actions.propose_claim_revision,
        a.get("_tool_use_id", ""),
        claim_id=a["claim_id"],
        changes=a["changes"],
        reason=a["reason"],
    )


def propose_new_claim(ctx: ToolContext, a: dict[str, Any]) -> ToolOutput:
    return _pending(
        ctx,
        actions.propose_new_claim,
        a.get("_tool_use_id", ""),
        library_path=a["library_path"],
        quantity=_quantity_arg(a),
        source_id=a["source_id"],
        quote=a["quote"],
        conditions=a["conditions"],
        species=a["species"],
        method=a["method"],
        mapping=a["mapping"],
    )


def propose_hypothesis(ctx: ToolContext, a: dict[str, Any]) -> ToolOutput:
    return _pending(
        ctx,
        actions.propose_hypothesis,
        a.get("_tool_use_id", ""),
        statement=a["statement"],
        prediction=a["prediction"],
        linked_claims=a["linked_claims"],
    )


def propose_library_change(ctx: ToolContext, a: dict[str, Any]) -> ToolOutput:
    def fn(store: Store, **kw: Any) -> PendingAction:
        return actions.propose_library_change(store, ctx.cfg, **kw)

    return _pending(
        ctx,
        fn,
        a.get("_tool_use_id", ""),
        claim_ids=a["claim_ids"],
        experiment_ids=a["experiment_ids"],
        rationale=a["rationale"],
    )


WRITE_TOOLS: tuple[ToolSpec, ...] = (
    ToolSpec(
        "propose_claim_revision",
        "write",
        "Propose a change to a claim: value, conditions, species, method, mapping, or status "
        "(disputed or extracted only). A new value needs the source and the verbatim quote that "
        "states it. Creates an Approve/Reject card; nothing changes until the person approves.",
        {
            "claim_id": _s("the claim to change"),
            "changes": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "conditions",
                    "high",
                    "low",
                    "mapping",
                    "method",
                    "quote",
                    "source_id",
                    "species",
                    "status",
                    "unit",
                    "value",
                ],
                "properties": {
                    **_Q,
                    "conditions": _ns("new conditions, or null to keep"),
                    "species": _ns("new species, or null to keep"),
                    "method": _enum_n(
                        ("behavioural", "electrophysiology", "anatomical", "model", "review"),
                        "new method, or null",
                    ),
                    "mapping": _enum_n(
                        ("exact", "close", "loose"),
                        "does the measured thing equal the library field",
                    ),
                    "status": _enum_n(("disputed", "extracted"), "new status, or null"),
                    "source_id": _ns("source of the quote"),
                    "quote": _ns("verbatim sentence from the fetched text that states the value"),
                },
            },
            "reason": _s("why, in the person's words where possible"),
        },
        propose_claim_revision,
        lambda a: f"Proposing a change to {a.get('claim_id')}",
    ),
    ToolSpec(
        "propose_new_claim",
        "write",
        "Propose a new claim from a fetched source. The quote must be in the fetched text and "
        "must state the value. Creates an Approve/Reject card.",
        {
            "library_path": _s("full library path"),
            **_Q,
            "source_id": _s("source with fetched text"),
            "quote": _s("verbatim sentence that states the value"),
            "conditions": _ns("measurement conditions"),
            "species": _ns("species measured"),
            "method": _enum_n(
                ("behavioural", "electrophysiology", "anatomical", "model", "review"), "method"
            ),
            "mapping": {"type": "string", "enum": ["exact", "close", "loose"]},
        },
        propose_new_claim,
        lambda a: f"Proposing new evidence for {a.get('library_path')}",
    ),
    ToolSpec(
        "propose_hypothesis",
        "write",
        "Propose a hypothesis with its prediction stated before any experiment is run. Creates "
        "an Approve/Reject card.",
        {
            "statement": _s("the hypothesis"),
            "prediction": _s("what bench result would support or refute it"),
            "linked_claims": _strs("claim ids it is about"),
        },
        propose_hypothesis,
        lambda a: "Proposing a hypothesis",
    ),
    ToolSpec(
        "propose_library_change",
        "write",
        "Propose changing the library to the given claims' values (they need verified quotes). "
        "On approval this becomes a pull request on profiles. Creates an Approve/Reject card.",
        {
            "claim_ids": _strs("claims with verified quotes"),
            "experiment_ids": _strs("experiments that tested the change (may be empty)"),
            "rationale": _s("the reviewer's reasoning"),
        },
        propose_library_change,
        lambda a: "Proposing a library change",
    ),
)

__all__ = ["WRITE_TOOLS"]
