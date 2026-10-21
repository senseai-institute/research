# SPDX-License-Identifier: Apache-2.0
"""Evidence → circuit inputs: quality, estimates, the package, and its approval-gated release."""

from __future__ import annotations

import importlib.util
import json
import math
import subprocess
from pathlib import Path
from typing import Any

import httpx
import jsonschema
import pytest
from fastapi.testclient import TestClient

from senseai_research import actions, handoff
from senseai_research.config import Settings
from senseai_research.handoff import deliver, rules
from senseai_research.handoff.estimate import evidence_for, pool
from senseai_research.handoff.quality import score
from senseai_research.ledger.models import (
    Approval,
    Claim,
    HandoffRelease,
    Passage,
    PendingAction,
    Quantity,
    Source,
    Vetting,
)
from senseai_research.ledger.store import LedgerError, Store
from senseai_research.proposals import github
from senseai_research.server.app import create_app
from tests.conftest import FIXTURES, PROFILES, git

GOLDEN = FIXTURES / "golden" / "handoff_felidae.json"
ACTOR = "pipeline:test@0"
HAVE_CIRCUITS = importlib.util.find_spec("senseai_circuits") is not None


@pytest.fixture(autouse=True)
def _fixed_sha(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RESEARCH_GIT_SHA", "test-sha")


def put(
    store: Store,
    path: str,
    *,
    value: float | None = None,
    low: float | None = None,
    high: float | None = None,
    unit: str = "deg",
    doi: str = "10.1/a",
    status: str = "unverified",
    species: str | None = "Felis catus",
    method: str | None = "behavioural",
    mapping: str | None = None,
    confidence: str | None = "high",
    vetting: Vetting | None = None,
    full: bool = False,
    n: int = 0,
) -> Claim:
    sid = f"src:{doi}"
    text = f"The value was {value} {unit}."
    if store.get("source", sid, Source) is None:
        store.put_source(
            Source(
                id=sid,
                created_by=ACTOR,
                doi=doi,
                title=f"Paper {doi}",
                year=2000,
                vetting=vetting,
                fulltext_available=full,
                fetched_text_sha256=store.store_text(text),
            ),
            ACTOR,
        )
    passage_id = None
    if status in ("extracted", "verified", "disputed"):
        src = store.must("source", sid, Source)
        stored = store.text(src.fetched_text_sha256 or "")
        assert stored is not None
        p = Passage(
            id=f"psg-{doi}-{n}",
            created_by=ACTOR,
            source_id=sid,
            char_start=0,
            char_end=9,
            text=stored[0][:9],
        )
        store.add_passage(p, ACTOR)
        passage_id = p.id
    c = Claim(
        id=f"clm:{path}:{doi}:{n}",
        created_by=ACTOR,
        library_path=path,
        quantity=Quantity(value=value, low=low, high=high, unit=unit),
        species=species,
        method=method,  # type: ignore[arg-type]
        source_ids=[sid],
        passage_id=passage_id,
        mapping=mapping,  # type: ignore[arg-type]
        status="unverified" if status == "verified" else status,  # type: ignore[arg-type]
        library_confidence=confidence,
    )
    store.create_claim(c, ACTOR)
    if status == "verified":
        ap = Approval(
            id=f"apr-{c.id}",
            created_by="user:t",
            target_kind="claim_revision",
            target_id="act-x",
            decision="approve",
            by="user:t",
            rationale="Checked it against the paper.",
        )
        store.record_approval(ap)
        c = store.revise_claim(
            c.id, {"status": "verified"}, reason="checked", by="user:t", approval_id=ap.id
        )
    return c


def _est(store: Store, knob: str, clade: str = "felidae") -> Any:
    rule = rules.rules_by_knob()[knob]
    ev = []
    for c in store.claims():
        if c.library_path == f"{clade}.{rule.leaf}":
            srcs = [s for sid in c.source_ids if (s := store.get("source", sid, Source))]
            ev.append(
                evidence_for(c, score(c, srcs, rule=rule, representative=["Felis catus"]), rule)
            )
    return pool(rules.knobs()[knob], rule, ev)


# -- rules ---------------------------------------------------------------------------------


def test_every_literature_knob_has_a_rule_or_a_reason() -> None:
    assert rules.check() == []


@pytest.mark.skipif(not HAVE_CIRCUITS, reason="senseai-circuits is not installed")
def test_registry_copy_matches_installed_circuits() -> None:
    from senseai_circuits.knobs import knobs_json  # type: ignore[import-not-found]

    assert json.loads(rules.registry_text()) == knobs_json()


def test_transforms() -> None:
    r = rules.rules_by_knob()
    assert r["cs.sigma_center_deg"].apply(1.0) == pytest.approx(1 / math.sqrt(2))
    assert r["emd.delay_tau_ms"].apply(4.547) == pytest.approx(35.0, rel=1e-3)
    assert r["lgmd.ttc_alpha"].apply(25.0) == pytest.approx(0.08)


# -- quality -------------------------------------------------------------------------------


def test_quality_gates(store: Store) -> None:
    c = put(
        store, "felidae.senses.hearing.spatial_resolution", value=5, vetting=Vetting(retracted=True)
    )
    q = score(c, [store.must("source", c.source_ids[0], Source)])
    assert not q.usable and "retracted" in (q.gate or "") and q.score == 0.0
    assert score(c, []).gate == "it cites no source the ledger knows"


def test_quality_explains_every_factor(store: Store) -> None:
    rule = rules.rules_by_knob()["ts.sigma_bearing_deg"]
    weak = put(
        store,
        "felidae.senses.hearing.spatial_resolution",
        value=5,
        confidence="medium",
        species="Panthera leo",
        method="electrophysiology",
        vetting=Vetting(flags=["predatory venue"]),
    )
    q = score(
        weak,
        [store.must("source", weak.source_ids[0], Source)],
        rule=rule,
        representative=["Felis catus"],
    )
    names = [f.name for f in q.factors]
    assert names == ["status", "reading", "method", "species", "library_confidence", "vetting"]
    product = 1.0
    for f in q.factors:
        product *= f.multiplier
        assert f.sentence.endswith(".")
    assert q.score == pytest.approx(product, abs=1e-4) and q.label == "low"

    strong = put(
        store,
        "felidae.senses.hearing.spatial_resolution",
        value=5,
        doi="10.1/b",
        status="verified",
        mapping="exact",
    )
    q2 = score(
        strong,
        [store.must("source", strong.source_ids[0], Source)],
        rule=rule,
        representative=["Felis catus"],
    )
    assert q2.score == 1.0 and q2.label == "high"


# -- estimates -----------------------------------------------------------------------------


def test_two_agreeing_verified_sources_are_ready(store: Store) -> None:
    for doi, v in (("10.1/a", 5.0), ("10.1/b", 5.5)):
        put(
            store,
            "felidae.senses.hearing.spatial_resolution",
            value=v,
            doi=doi,
            status="verified",
            mapping="exact",
        )
    e = _est(store, "ts.sigma_bearing_deg")
    assert e.readiness == "ready" and e.agreement == "agree"
    assert e.value == pytest.approx(5.25) and (e.low, e.high) == (5.0, 5.5)
    assert e.strength == pytest.approx(0.9) and e.independent_sources == 2


def test_one_unverified_source_is_provisional(store: Store) -> None:
    put(store, "felidae.senses.hearing.spatial_resolution", value=5)
    e = _est(store, "ts.sigma_bearing_deg")
    assert e.readiness == "provisional" and e.agreement == "single"
    assert any("second" in s for s in e.sentences)


def test_disagreement_halves_strength(store: Store) -> None:
    put(
        store, "felidae.senses.hearing.spatial_resolution", value=2, doi="10.1/a", status="verified"
    )
    put(
        store, "felidae.senses.hearing.spatial_resolution", value=8, doi="10.1/b", status="verified"
    )
    e = _est(store, "ts.sigma_bearing_deg")
    assert e.agreement == "disagree" and e.readiness == "provisional"
    assert e.strength == pytest.approx(0.45)


def test_units_ranges_and_out_of_range(store: Store) -> None:
    put(store, "felidae.senses.hearing.spatial_resolution", low=120, high=240, unit="arcmin")
    e = _est(store, "ts.sigma_bearing_deg")
    assert (e.low, e.value, e.high) == (2.0, 3.0, 4.0)
    put(store, "felidae.senses.vision.motion_tf_optimum", value=0.1, unit="Hz")
    far = _est(store, "emd.delay_tau_ms")  # 1592 ms: above circuits' 1000 ms ceiling
    assert far.readiness == "insufficient" and not far.in_range and far.strength == 0.0
    put(store, "felidae.senses.vision.rgc_surround_ratio", value=5, unit="Hz")
    bad = _est(store, "cs.surround_ratio")
    assert bad.value is None and "cannot be put in ratio" in (bad.evidence[0].excluded or "")


# -- the package ---------------------------------------------------------------------------


def _strigid(store: Store) -> None:
    put(store, "strigidae.senses.hearing.spatial_resolution", value=2, species="Tyto alba")


def test_package_is_deterministic_and_valid(store: Store, settings: Settings) -> None:
    _strigid(store)
    a = handoff.build(store, settings, "strigidae")
    b = handoff.build(store, settings, "strigidae")
    assert a.text() == b.text() and a.digest == a.compute_digest()
    doc = json.loads(a.text())
    jsonschema.validate(doc, handoff.schema())
    assert handoff.verify(doc).digest == a.digest
    doc["circuits"]["ts.sigma_bearing_deg"] = 9.0
    with pytest.raises(handoff.HandoffError, match="digest"):
        handoff.verify(doc)
    assert a.circuits == {"ts.sigma_bearing_deg": 2.0}
    kinds = {g.kind for g in a.gaps}
    assert kinds == {"no_evidence", "no_literature"}
    assert set(a.engineering_knobs).isdisjoint(k.knob for k in a.knobs)
    assert list(a.sources) == ["src:10.1/a"]


def test_policy_ready_passes_nothing_provisional(store: Store, settings: Settings) -> None:
    _strigid(store)
    h = handoff.build(store, settings, "strigidae", policy=handoff.Policy(min_readiness="ready"))
    assert h.circuits == {}
    assert any(g.kind == "below_policy" for g in h.gaps)


def test_unknown_clade(store: Store, settings: Settings) -> None:
    with pytest.raises(handoff.HandoffError):
        handoff.build(store, settings, "canidae")


@pytest.mark.skipif(not HAVE_CIRCUITS, reason="senseai-circuits is not installed")
def test_circuits_reads_the_package(store: Store, settings: Settings) -> None:
    from senseai_circuits.params import CircuitParams  # type: ignore[import-not-found]

    _strigid(store)
    doc = json.loads(handoff.build(store, settings, "strigidae").text())
    p = CircuitParams.from_bundle_meta(doc)
    assert p.ts_sigma_bearing_deg == 2.0
    assert CircuitParams.from_profile({"circuits": doc["circuits"]}) == p


@pytest.mark.profiles
def test_golden_felidae(imported: Store, settings: Settings) -> None:
    """The fixture library's felidae package. Regenerate: SENSEAI_UPDATE_GOLDEN=1 pytest -k."""
    import os

    doc = json.loads(handoff.build(imported, settings, "felidae").text())
    # the fixture library sits inside this repository, so its SHA changes with every commit
    doc["provenance"]["profiles_sha"] = doc["digest"] = "-"
    text = json.dumps(doc, indent=1, sort_keys=True) + "\n"
    if os.environ.get("SENSEAI_UPDATE_GOLDEN"):
        GOLDEN.write_text(text)
    assert text == GOLDEN.read_text()
    assert doc["circuits"] == {"ts.sigma_bearing_deg": 5.0}
    sb = next(k for k in doc["knobs"] if k["knob"] == "ts.sigma_bearing_deg")
    assert sb["estimate"]["readiness"] == "provisional"


# -- release -------------------------------------------------------------------------------


def test_release_needs_a_person(store: Store, settings: Settings) -> None:
    _strigid(store)
    a = actions.propose_handoff(store, settings, clade="strigidae", actor="agent:c1")
    assert a.kind == "handoff" and a.card["rows"][0]["after"] == "2.0"
    assert not deliver.outbox(settings).exists()
    rej = Approval(
        id="apr-r",
        created_by="user:t",
        target_kind="handoff",
        target_id=a.id,
        decision="reject",
        by="user:t",
        rationale="Not this one, please.",
    )
    h = handoff.build(store, settings, "strigidae")
    with pytest.raises(LedgerError, match="N1"):
        deliver.release(store, settings, a, rej, h)
    out = actions.approve(
        store, settings, a.id, by="user:kedar", rationale="The owl value is the one we want."
    )
    assert out.action.status == "approved", out.message
    rel = deliver.latest(store, "strigidae")
    assert rel is not None and rel.by == "user:kedar" and rel.pr_url is None
    doc = json.loads((settings.data / rel.file).read_text())
    assert handoff.verify(doc).digest == rel.digest == a.input["digest"]


def test_changed_evidence_is_not_released(store: Store, settings: Settings) -> None:
    _strigid(store)
    a = actions.propose_handoff(store, settings, clade="strigidae", actor="user:t")
    put(store, "strigidae.senses.hearing.spatial_resolution", value=3, doi="10.1/z")
    out = actions.approve(store, settings, a.id, by="user:t", rationale="Release it to circuits.")
    assert out.action.status == "failed" and "changed" in (out.action.error or "")
    assert deliver.latest(store, "strigidae") is None


def test_release_opens_a_pr_on_circuits(store: Store, settings: Settings, tmp_path: Path) -> None:
    seed = tmp_path / "circuits-seed"
    seed.mkdir()
    (seed / "README.md").write_text("circuits\n")
    git("init", "-q", "-b", "main", cwd=seed)
    git("-c", "user.name=t", "-c", "user.email=t@t", "add", "-A", cwd=seed)
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "base", cwd=seed)
    remote = tmp_path / "circuits.git"
    git("clone", "-q", "--bare", str(seed), str(remote), cwd=tmp_path)
    calls: list[dict[str, Any]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append({"url": str(req.url), "json": json.loads(req.content)})
        return httpx.Response(201, json={"html_url": "https://github.com/o/circuits/pull/7"})

    github.transport = httpx.MockTransport(handler)
    try:
        cfg = settings.with_(
            circuits_repo="o/circuits", circuits_remote=str(remote), circuits_token="tok-c"
        )
        _strigid(store)
        a = actions.propose_handoff(store, cfg, clade="strigidae", actor="user:t")
        out = actions.approve(store, cfg, a.id, by="user:t", rationale="Send it to circuits.")
    finally:
        github.transport = None
    assert out.action.status == "approved" and out.action.result["pr_url"].endswith("/7")
    (call,) = calls
    assert call["url"].endswith("/repos/o/circuits/pulls")
    branch = call["json"]["head"]
    assert branch.startswith("research/handoff-strigidae-") and call["json"]["base"] == "main"
    assert "ts.sigma_bearing_deg" in call["json"]["body"]
    shown = subprocess.run(
        ["git", "--git-dir", str(remote), "show", f"{branch}:research/strigidae.handoff.json"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert handoff.verify(json.loads(shown)).circuits == {"ts.sigma_bearing_deg": 2.0}
    main = git("--git-dir", str(remote), "rev-parse", "main", cwd=tmp_path)
    assert main == git("-C", str(seed), "rev-parse", "HEAD", cwd=tmp_path)  # main untouched


# -- API -----------------------------------------------------------------------------------


def test_api(store: Store, settings: Settings) -> None:
    _strigid(store)
    app = create_app(
        settings, store=store, trusted_hosts=("testclient",), model_check=lambda: (True, "ok")
    )
    with TestClient(app) as c:
        (row,) = c.get("/api/research/handoff").json()
        assert row["clade"] == "strigidae" and row["released"] is None
        draft = c.get("/api/research/handoff/strigidae").json()
        jsonschema.validate(draft, c.get("/api/research/schema/handoff").json())
        assert c.get("/api/research/handoff/canidae").status_code == 404
        assert c.get("/api/research/handoff/strigidae/released").status_code == 404
        assert c.post("/api/research/handoff/strigidae/propose", json={}).status_code == 401
        act = c.post(
            "/api/research/handoff/strigidae/propose", json={}, headers={"X-SenseAI-User": "kedar"}
        ).json()
        r = c.post(
            f"/api/research/actions/{act['id']}/approve",
            json={"rationale": "Ready for the owl model."},
            headers={"X-SenseAI-User": "kedar"},
        )
        assert r.status_code == 200 and r.json()["action"]["status"] == "approved"
        got = c.get("/api/research/handoff/strigidae/released").json()
        assert got["digest"] == draft["digest"]
        cid = store.claims()[0].id
        q = c.get(f"/api/research/claims/{cid}").json()["quality"]
        assert q["sets_knob"] == "ts.sigma_bearing_deg" and q["label"] in ("medium", "low")
    assert store.all("handoff", HandoffRelease)[0].clade == "strigidae"
    assert store.all("action", PendingAction)[0].status == "approved"


def test_cli(
    store: Store,
    settings: Settings,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from senseai_research import cli

    _strigid(store)
    monkeypatch.setenv("RESEARCH_DATA", str(settings.data))
    monkeypatch.setenv("SENSEAI_PROFILES_REPO", str(PROFILES))
    out = tmp_path / "pkg.json"
    assert cli.main(["handoff", "strigidae", "--out", str(out)]) == 0
    text = capsys.readouterr().out
    assert "ts.sigma_bearing_deg" in text and "Gaps:" in text
    assert handoff.verify(json.loads(out.read_text())).clade == "strigidae"
