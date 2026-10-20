# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from senseai_research.ledger.models import (
    Approval,
    Claim,
    Passage,
    Proposal,
    Quantity,
    Source,
    new_id,
)
from senseai_research.ledger.store import LedgerError, Store

USER = "user:senseai"


def _claim(store: Store, **kw: object) -> Claim:
    c = Claim(
        id=str(kw.pop("id", "clm-1")),
        created_by=USER,
        library_path="felidae.senses.vision.detection_threshold",
        quantity=Quantity(value=-0.74, unit="log10 re human"),
        **kw,  # type: ignore[arg-type]
    )
    return store.create_claim(c, USER)


def _source_with_passage(store: Store) -> Passage:
    text = "Cats detect gratings at 0.74 log units below the human threshold in scotopic light."
    sha = store.store_text(text)
    store.put_source(
        Source(id="src:x", created_by=USER, doi="10.1/x", fetched_text_sha256=sha), USER
    )
    start = text.index("0.74")
    p = Passage(
        id="psg-1",
        created_by=USER,
        source_id="src:x",
        char_start=start,
        char_end=start + 30,
        text=text[start : start + 30],
    )
    return store.add_passage(p, USER)


def _approve(store: Store, target: str = "act-1") -> Approval:
    return store.record_approval(
        Approval(
            id=new_id("apr"),
            created_by=USER,
            target_kind="claim_revision",
            target_id=target,
            decision="approve",
            by=USER,
            rationale="The camera's lens replaces the cat's pupil.",
        )
    )


def test_n3_events_table_refuses_update_and_delete(store: Store) -> None:
    _claim(store)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        store._conn.execute("UPDATE events SET actor='x'")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        store._conn.execute("DELETE FROM events")


def test_n3_revision_keeps_old_value_and_reason(store: Store) -> None:
    _claim(store)
    store.revise_claim(
        "clm-1",
        {"quantity": Quantity(value=0.0, unit="log10 re human").model_dump()},
        reason="optics factored out",
        by=USER,
    )
    c = store.must("claim", "clm-1", Claim)
    assert c.quantity.value == 0.0 and c.revision == 1
    (rev,) = store.claim_history("clm-1")
    assert rev.changes[0].field == "quantity"
    assert rev.changes[0].old["value"] == -0.74
    assert rev.reason == "optics factored out" and rev.by == USER
    created = store.claim_created_event("clm-1")
    assert created is not None and created.payload["claim"]["quantity"]["value"] == -0.74


def test_n3_projection_is_a_fold_of_the_log(store: Store) -> None:
    _claim(store)
    store.revise_claim("clm-1", {"species": "Felis catus"}, reason="species", by=USER)
    before = store.snapshot()
    store.rebuild()
    assert store.snapshot() == before


def test_n3_fetched_texts_are_immutable(store: Store) -> None:
    sha = store.store_text("some text")
    with pytest.raises(sqlite3.IntegrityError):
        store._conn.execute("UPDATE source_texts SET text='other' WHERE sha256=?", (sha,))


def test_n1_agent_cannot_change_claims(store: Store) -> None:
    _claim(store)
    with pytest.raises(LedgerError, match="N1"):
        store.revise_claim("clm-1", {"status": "disputed"}, reason="r", by="agent:conv-1")
    with pytest.raises(LedgerError, match="N1"):
        store.create_claim(
            Claim(
                id="clm-2",
                created_by="agent:conv-1",
                library_path="felidae.senses.vision.detection_threshold",
                quantity=Quantity(value=1, unit="x"),
            ),
            "agent:conv-1",
        )


def test_n1_verify_needs_a_person_and_an_approval(store: Store) -> None:
    p = _source_with_passage(store)
    _claim(store, passage_id=p.id, status="extracted")
    with pytest.raises(LedgerError, match="needs an Approval"):
        store.revise_claim("clm-1", {"status": "verified"}, reason="checked", by=USER)
    with pytest.raises(LedgerError, match="only a person"):
        store.revise_claim(
            "clm-1", {"status": "verified"}, reason="checked", by="pipeline:x@1", approval_id="a"
        )
    ap = _approve(store)
    c = store.revise_claim(
        "clm-1", {"status": "verified"}, reason="checked", by=USER, approval_id=ap.id
    )
    assert c.status == "verified"


def test_n1_approval_must_be_by_a_person_with_a_rationale() -> None:
    with pytest.raises(ValueError, match="only a person"):
        Approval(
            id="a",
            created_by="agent:c",
            target_kind="proposal",
            target_id="x",
            decision="approve",
            by="agent:c",
            rationale="looks good to me",
        )
    with pytest.raises(ValueError, match="sentence"):
        Approval(
            id="a",
            created_by=USER,
            target_kind="proposal",
            target_id="x",
            decision="approve",
            by=USER,
            rationale="yes",
        )


def test_n2_extracted_claim_needs_a_passage(store: Store) -> None:
    with pytest.raises(ValueError, match="N2"):
        _claim(store, status="extracted")


def test_n2_passage_must_be_in_the_fetched_text(store: Store) -> None:
    _source_with_passage(store)
    with pytest.raises(LedgerError, match="not at the given offsets"):
        store.add_passage(
            Passage(
                id="psg-2",
                created_by=USER,
                source_id="src:x",
                char_start=0,
                char_end=10,
                text="invented!!",
            ),
            USER,
        )


def test_n2_unverified_claim_cannot_be_proposed(store: Store) -> None:
    _claim(store)
    with pytest.raises(LedgerError, match="N2"):
        store.put_proposal(
            Proposal(
                id="prp-1",
                created_by=USER,
                claim_ids=["clm-1"],
                library_file="library/felidae.yaml",
                yaml_patch="",
                rationale="r",
            ),
            USER,
        )


def test_schema_migrations_are_recorded(store: Store) -> None:
    assert store.schema_version == 2
    again = Store(store.path)
    assert again.schema_version == 2
    again.close()


def test_committed_json_schemas_match_the_models(tmp_path: Path) -> None:
    from senseai_research.cli import main

    main(["--data", str(tmp_path), "schema", "--out", str(tmp_path / "schema")])
    repo = Path(__file__).resolve().parents[3] / "schema"
    for f in sorted((tmp_path / "schema").glob("*.json")):
        assert f.read_text() == (repo / f.name).read_text(), (
            f"run `senseai-research schema`: {f.name}"
        )
