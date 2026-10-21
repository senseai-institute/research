# SPDX-License-Identifier: Apache-2.0
"""Circuit candidates: scores from the ledger, circuitability from senseai-circuits, decisions."""

from __future__ import annotations

import os
import shlex
import shutil
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from senseai_research import candidates
from senseai_research.cli import main
from senseai_research.config import Settings
from senseai_research.ledger.models import CircuitCandidate, Claim
from senseai_research.ledger.store import LedgerError, Store
from senseai_research.server.app import create_app
from tests.conftest import FIXTURES, HAVE_PROFILES

pytestmark = pytest.mark.skipif(not HAVE_PROFILES, reason="needs senseai-profiles")

STUB = f"{sys.executable} {FIXTURES / 'stub_circuits.py'}"
HEARING = "cc:felidae.hearing:felidae.senses.hearing.spatial_resolution"
ALICE = {"X-SenseAI-User": "alice"}


OWL = """clade: Strigidae
representative_species:
  - name: Tyto alba
senses:
  - id: hearing
    modality: hearing
"""


@pytest.fixture
def cfg(settings: Settings, tmp_path: Path) -> Settings:
    """The fixture profiles plus an owl entry with a hearing sense (for cross-clade candidates)."""
    root = tmp_path / "profiles"
    shutil.copytree(settings.profiles_repo, root)  # type: ignore[arg-type]
    (root / "library" / "strigidae.yaml").write_text(OWL)
    return settings.with_(circuits_cmd=STUB, profiles_repo=root)


def test_scores_come_from_the_ledger(imported: Store) -> None:
    c = imported.must("claim", "lib:felidae.senses.hearing.spatial_resolution", Claim)
    q, parts = candidates.quality(imported, c)
    assert parts["status"] == {"value": "unverified", "weight": 0.25}
    assert parts["confidence"]["weight"] == 0.7  # library confidence: medium
    assert (
        parts["read"]["value"] == "abstract + secondary review" and parts["read"]["weight"] == 0.7
    )
    assert q == pytest.approx(0.25 * 0.7 * parts["method"]["weight"] * 0.7, abs=1e-4)
    lk, lp = candidates.likelihood(imported, c, "felidae", ["Felis catus"])
    assert lk == 1.0 and lp["species"]["value"] == "same species"
    other, op = candidates.likelihood(imported, c, "hominidae", ["Homo sapiens"])
    assert other == 0.35 and op["species"]["value"] == "other clade"
    assert (
        candidates.species_relation("Felis silvestris", "felidae", "felidae", ["Felis catus"])
        == "same genus"
    )
    assert (
        candidates.species_relation("Lynx lynx", "felidae", "felidae", ["Felis catus"])
        == "same clade"
    )
    assert candidates.species_relation(None, "felidae", "felidae", []) == "species not recorded"


def test_a_disputed_claim_on_the_path_lowers_likelihood(
    imported: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    c = imported.must("claim", "lib:felidae.senses.hearing.spatial_resolution", Claim)
    rival = Claim.model_construct(**(c.model_dump() | {"id": "lib:x", "status": "disputed"}))
    monkeypatch.setattr(imported, "claims", lambda: [c, rival])
    lk, lp = candidates.likelihood(imported, c, "felidae", ["Felis catus"])
    assert lk == pytest.approx(0.7) and lp["agreement"]["value"] == "disputed"


def test_derive_stores_circuit_material_with_circuitability(imported: Store, cfg: Settings) -> None:
    res = candidates.derive(imported, cfg)
    assert res["circuits"] == "checked" and res["circuits_version"] == "0.0-stub"
    rows = candidates.ranked(imported)
    ids = [c.id for c in rows]
    assert ids[0] == HEARING  # the one knob-level candidate ranks first
    top = rows[0]
    assert top.circuitability and top.circuitability["knob"] == "ts.sigma_bearing_deg"
    assert top.priority == pytest.approx(top.likelihood * top.quality)
    levels = {c.circuitability["level"] for c in rows if c.circuitability}
    assert "none" not in levels and {"knob", "circuit", "new"} <= levels
    # a value from another clade is kept only when circuits says it sets a knob
    cross = [c for c in rows if c.evidence_clade != c.clade]
    assert cross and all(c.circuitability and c.circuitability["level"] == "knob" for c in cross)
    assert all(c.likelihood <= 0.35 for c in cross)
    assert candidates.ranked(imported, "felidae", "hearing")[0].id == HEARING
    n_events = len(imported.events(type_prefix="candidate."))
    assert candidates.derive(imported, cfg)["stored"] == res["stored"]
    assert len(imported.events(type_prefix="candidate.")) == n_events  # unchanged: no new events


def test_without_circuits_candidates_are_unchecked(imported: Store, settings: Settings) -> None:
    res = candidates.derive(imported, settings.with_(circuits_cmd="no-such-senseai-circuits"))
    assert "not found" in res["circuits"]
    rows = candidates.ranked(imported)
    assert rows and all(c.circuitability is None and c.priority is None for c in rows)
    assert all(c.evidence_clade == c.clade for c in rows)
    assert "not checked" in rows[0].circuits_note


def test_circuits_format_and_decisions(imported: Store, cfg: Settings) -> None:
    candidates.derive(imported, cfg)
    c = imported.must("candidate", HEARING, CircuitCandidate)
    rec = candidates.to_circuits(imported, c)
    assert rec["schema"] == "senseai-circuits/candidate@1" and rec["modality"] == "hearing"
    assert rec["evidence"][0]["value"] == 5.0 and rec["evidence"][0]["unit"] == "deg"
    assert set(rec["scores"]) == {"likelihood", "quality", "parts"}
    with pytest.raises(LedgerError, match="person"):
        candidates.decide(imported, HEARING, "in_design", by="agent:x", rationale="I think so too.")
    with pytest.raises(LedgerError, match="sentence"):
        candidates.decide(imported, HEARING, "in_design", by="user:alice", rationale="ok")
    with pytest.raises(LedgerError, match="one of"):
        candidates.decide(
            imported, HEARING, "merged", by="user:alice", rationale="it is merged now"
        )
    d = candidates.decide(
        imported,
        HEARING,
        "in_design",
        by="user:alice",
        rationale="Designing it in Studio.",
        design_id="dz-1",
    )
    assert d.status == "in_design" and d.design_ids == ["dz-1"] and d.decided_by == "user:alice"
    candidates.derive(imported, cfg)  # re-scoring keeps the people's side
    again = imported.must("candidate", HEARING, CircuitCandidate)
    assert again.status == "in_design" and again.design_ids == ["dz-1"]


def test_api_lists_details_refreshes_and_decides(imported: Store, cfg: Settings) -> None:
    app = create_app(
        cfg, store=imported, trusted_hosts=("testclient",), model_check=lambda: (True, "ok")
    )
    with TestClient(app) as client:
        assert client.post("/api/research/circuit-candidates/refresh").status_code == 401
        r = client.post("/api/research/circuit-candidates/refresh", headers=ALICE)
        assert r.status_code == 200 and r.json()["stored"] > 0
        rows = client.get("/api/research/circuit-candidates", params={"clade": "felidae"}).json()
        assert rows[0]["id"] == HEARING and all(x["clade"] == "felidae" for x in rows)
        one = client.get(f"/api/research/circuit-candidates/{HEARING}").json()
        assert one["claims"][0]["claim_id"] == "lib:felidae.senses.hearing.spatial_resolution"
        assert one["claims"][0]["sources"] and one["candidate"]["id"] == HEARING
        assert client.get("/api/research/circuit-candidates/cc:nope").status_code == 404
        body = {
            "status": "adopted",
            "rationale": "Adopted as design dz-1 in Studio.",
            "design_id": "dz-1",
        }
        assert (
            client.post(
                f"/api/research/circuit-candidates/{HEARING}/decision", json=body
            ).status_code
            == 401
        )
        ok = client.post(
            f"/api/research/circuit-candidates/{HEARING}/decision", json=body, headers=ALICE
        )
        assert ok.status_code == 200 and ok.json()["status"] == "adopted"
        bad = client.post(
            f"/api/research/circuit-candidates/{HEARING}/decision",
            json=body | {"rationale": "no"},
            headers=ALICE,
        )
        assert bad.status_code == 400
        assert (
            client.get("/api/research/circuit-candidates", params={"status": "adopted"}).json()[0][
                "id"
            ]
            == HEARING
        )
        assert client.get("/api/research/health").json()["circuits"] is True


def test_cli_candidates(
    imported: Store,
    cfg: Settings,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    imported.close()
    monkeypatch.setenv("RESEARCH_DATA", str(cfg.data))
    monkeypatch.setenv("SENSEAI_PROFILES_REPO", str(cfg.profiles_repo))
    monkeypatch.setenv("SENSEAI_CIRCUITS_CMD", STUB)
    assert main(["candidates", "--refresh", "--clade", "felidae"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("stored ") and "felidae.senses.hearing.spatial_resolution" in out
    assert main(["candidates", "--json", "--sense", "hearing"]) == 0
    assert '"senseai-circuits/candidate@1"' in capsys.readouterr().out


REAL = os.environ.get("SENSEAI_CIRCUITS_CMD", "senseai-circuits")


@pytest.mark.skipif(not shutil.which(shlex.split(REAL)[0]), reason="senseai-circuits not installed")
def test_against_the_real_circuits(imported: Store, settings: Settings) -> None:
    res = candidates.derive(imported, settings.with_(circuits_cmd=REAL))
    assert res["circuits"] == "checked" and res["circuits_version"]
    top = candidates.ranked(imported, "felidae", "hearing")[0]
    assert top.circuitability and top.circuitability["knob"] == "ts.sigma_bearing_deg"
    assert top.circuitability["value"] == 5.0


def test_schema_file_is_written(tmp_path: Path) -> None:
    assert main(["schema", "--out", str(tmp_path)]) == 0
    assert (tmp_path / "circuitcandidate.schema.json").is_file()
