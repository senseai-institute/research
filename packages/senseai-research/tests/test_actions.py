# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

from pathlib import Path

import pytest

from senseai_research import actions
from senseai_research.config import Settings
from senseai_research.ledger.models import (
    Approval,
    Claim,
    Hypothesis,
    Passage,
    PendingAction,
    Proposal,
)
from senseai_research.ledger.store import Store
from senseai_research.proposals import github
from tests.conftest import KANG_SOURCE, OPTICS_QUOTE, git, store_kang_text

pytestmark = pytest.mark.profiles

CLAIM = "lib:felidae.senses.vision.detection_threshold"
AGENT = "agent:conv-1"
USER = "user:senseai"
WHY = "For a camera the lens replaces the cat's pupil, so the optics-removed value applies."


def _revision(store: Store) -> PendingAction:
    return actions.propose_claim_revision(
        store,
        claim_id=CLAIM,
        changes={
            "value": -0.08,
            "unit": "log10 re human",
            "conditions": "Behavioural, scotopic, in retinal illuminance (optics removed).",
            "mapping": "close",
            "status": "extracted",
            "source_id": KANG_SOURCE,
            "quote": OPTICS_QUOTE,
        },
        reason="The user asked for the optics-removed value for a robot camera.",
        actor=AGENT,
        conversation_id="conv-1",
    )


@pytest.fixture
def ready(imported: Store) -> Store:
    store_kang_text(imported)
    return imported


def test_n1_proposal_changes_nothing_until_a_person_approves(
    ready: Store, settings: Settings
) -> None:
    before = ready.must("claim", CLAIM, Claim)
    n_events = len(ready.events(type_prefix="claim."))
    a = _revision(ready)
    assert a.status == "pending" and a.card["rows"][0]["after"] == "-0.08 log10 re human"
    assert ready.must("claim", CLAIM, Claim) == before
    assert len(ready.events(type_prefix="claim.")) == n_events
    out = actions.approve(ready, settings, a.id, by=USER, rationale=WHY)
    assert out.action.status == "approved"
    c = ready.must("claim", CLAIM, Claim)
    assert c.quantity.value == -0.08 and c.status == "extracted" and c.revision == 1
    assert c.provenance == "agent-proposed" and WHY in c.notes_by_reviewer
    (rev,) = ready.claim_history(CLAIM)
    assert rev.approval_id == out.approval.id and rev.by == USER
    assert next(ch for ch in rev.changes if ch.field == "quantity").old["value"] == -0.74
    p = ready.must("passage", c.passage_id or "", Passage)
    assert p.text == OPTICS_QUOTE and p.section == "Results"
    ap = ready.must("approval", out.approval.id, Approval)
    assert ap.by == USER and ap.rationale == WHY and ap.target_id == a.id
    assert "senseai approved" in out.message and "revision 1" in out.message


def test_n1_agent_cannot_ask_for_verified(ready: Store) -> None:
    with pytest.raises(actions.ActionError, match="set by the reviewer"):
        actions.propose_claim_revision(
            ready, claim_id=CLAIM, changes={"status": "verified"}, reason="r", actor=AGENT
        )


def test_n1_reject_changes_nothing(ready: Store) -> None:
    a = _revision(ready)
    out = actions.reject(ready, a.id, by=USER, rationale="The pupil value is the one we want.")
    assert out.action.status == "rejected" and out.approval.decision == "reject"
    assert ready.must("claim", CLAIM, Claim).revision == 0
    with pytest.raises(actions.ActionError, match="already rejected"):
        actions.approve(ready, Settings(data=Path("/nonexistent")), a.id, by=USER, rationale=WHY)


def test_n2_invented_quote_is_refused(ready: Store) -> None:
    with pytest.raises(actions.ActionError, match="not in the fetched text"):
        actions.propose_claim_revision(
            ready,
            claim_id=CLAIM,
            changes={"value": -0.08, "source_id": KANG_SOURCE, "quote": "Cats see 0.08 better."},
            reason="r",
            actor=AGENT,
        )


def test_n2_value_must_be_in_the_quote(ready: Store) -> None:
    with pytest.raises(actions.ActionError, match="does not appear in the quote"):
        actions.propose_claim_revision(
            ready,
            claim_id=CLAIM,
            changes={"value": 0.0, "source_id": KANG_SOURCE, "quote": OPTICS_QUOTE},
            reason="r",
            actor=AGENT,
        )
    with pytest.raises(actions.ActionError, match="needs the quote"):
        actions.propose_claim_revision(
            ready, claim_id=CLAIM, changes={"value": 0.0}, reason="r", actor=AGENT
        )


def test_n2_unverified_claim_cannot_go_to_the_library(ready: Store, settings: Settings) -> None:
    with pytest.raises(actions.ActionError, match="no verified quote"):
        actions.propose_library_change(
            ready, settings, claim_ids=[CLAIM], experiment_ids=[], rationale="r", actor=AGENT
        )


def test_new_claim_and_hypothesis(ready: Store, settings: Settings) -> None:
    a = actions.propose_new_claim(
        ready,
        library_path="felidae.senses.vision.detection_threshold",
        quantity={"value": -0.08, "unit": "log10 re human"},
        source_id=KANG_SOURCE,
        quote=OPTICS_QUOTE,
        conditions="retinal illuminance",
        species="Felis catus",
        method="behavioural",
        mapping="close",
        actor=AGENT,
    )
    out = actions.approve(ready, settings, a.id, by=USER, rationale="This is the right row.")
    c = ready.must("claim", out.action.result["claim_id"], Claim)
    assert c.status == "extracted" and c.provenance == "agent-proposed" and c.passage_id
    h = actions.propose_hypothesis(
        ready,
        statement="The optics-removed threshold improves Panther at night.",
        prediction="panther_prior hit rate rises above its chance rate on night recordings.",
        linked_claims=[CLAIM],
        actor=AGENT,
        conversation_id="conv-1",
    )
    out = actions.approve(ready, settings, h.id, by=USER, rationale="Worth testing on real data.")
    hyp = ready.must("hypothesis", out.action.result["hypothesis_id"], Hypothesis)
    assert hyp.origin == "conversation:conv-1" and hyp.status == "open"


def _approved_claim(store: Store, settings: Settings) -> None:
    a = _revision(store)
    actions.approve(store, settings, a.id, by=USER, rationale=WHY)


def test_library_change_without_token_goes_to_the_outbox(ready: Store, settings: Settings) -> None:
    _approved_claim(ready, settings)
    a = actions.propose_library_change(
        ready,
        settings,
        claim_ids=[CLAIM],
        experiment_ids=[],
        rationale="Use the optics-removed value.",
        actor=AGENT,
    )
    assert a.card["title"] == "library(felidae): senses.vision.detection_threshold -0.74 → -0.08"
    assert "+      value: -0.08" in a.card["diff"]
    out = actions.approve(
        ready, settings, a.id, by=USER, rationale="Agreed after the impact check."
    )
    assert out.action.status == "approved", out.action.error
    prop = ready.must("proposal", out.action.result["proposal_id"], Proposal)
    assert prop.status == "approved" and prop.pr_url is None and prop.action_id == a.id
    body = Path(prop.pr_body_file or "").read_text()
    assert OPTICS_QUOTE in body and "10.1152/jn.90641.2008" in body
    assert "Agreed after the impact check." in body and "Use the optics-removed value." in body
    assert "panther.frame.gain_lut" in body and "does not reach the world model yet" in body
    assert "Approved by senseai at" in body
    patch = Path(prop.patch_file or "").read_text()
    assert "-      value: -0.74" in patch and "evidence:" in patch and OPTICS_QUOTE in patch
    assert "saved for download" in out.message


def test_library_change_with_token_opens_a_pr(
    ready: Store, fake_github: tuple[Settings, Path, list[dict[str, object]]]
) -> None:
    cfg, remote, calls = fake_github
    head = git("rev-parse", "HEAD", cwd=cfg.profiles_repo or Path())
    _approved_claim(ready, cfg)
    a = actions.propose_library_change(
        ready,
        cfg,
        claim_ids=[CLAIM],
        experiment_ids=[],
        rationale="Optics-removed value.",
        actor=AGENT,
    )
    out = actions.approve(ready, cfg, a.id, by=USER, rationale="Approved after review.")
    assert out.action.status == "approved", out.action.error
    prop = ready.must("proposal", out.action.result["proposal_id"], Proposal)
    assert prop.status == "pr_open" and prop.pr_url and prop.pr_url.endswith("/pull/99")
    (call,) = calls
    body = call["json"]
    assert isinstance(body, dict)
    assert call["url"] == "https://api.github.com/repos/senseai-institute/profiles/pulls"
    assert call["auth"] == "Bearer tok-123"
    assert body["head"] == f"research/{prop.id}" and body["base"] == "main"
    assert body["title"] == "library(felidae): senses.vision.detection_threshold -0.74 → -0.08"
    pushed = git("show", f"research/{prop.id}:library/felidae.yaml", cwd=remote)
    assert "value: -0.08" in pushed
    checkout = cfg.profiles_repo or Path()
    assert git("rev-parse", "HEAD", cwd=checkout) == head
    assert git("status", "--porcelain", cwd=checkout) == ""


def test_n1_github_refuses_a_proposal_without_approval(
    ready: Store, fake_github: tuple[Settings, Path, list[dict[str, object]]]
) -> None:
    cfg, remote, calls = fake_github
    prop = Proposal(
        id="prp-x",
        created_by=USER,
        claim_ids=[CLAIM],
        library_file="library/felidae.yaml",
        yaml_patch="",
        rationale="r",
    )
    with pytest.raises(github.GitHubError, match="no Approval row"):
        github.open_pr(ready, cfg, prop, file="library/felidae.yaml", new_text="x", body="b")
    assert calls == []
    assert "research/" not in git("branch", "--list", cwd=remote)
