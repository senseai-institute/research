from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest
import yaml
from senseai_profiles.library import LibraryError, load_entry, validate_entry

from senseai_research.curate import assemble, extract, pipeline, review
from senseai_research.curate import cli as curate_cli
from senseai_research.curate.cache import CachedClient, ModelCache, RateLimiter
from senseai_research.curate.catalogue import catalogue_for
from senseai_research.curate.reconcile import reconcile
from tests.conftest import PROFILES
from tests.curate.fixtures import (
    DOI_GUNTER,
    DOI_KANG,
    DOI_RETRACTED,
    DOI_STEINBERG,
    KANG_FULLTEXT,
    transport,
)

REPO = PROFILES  # a small profiles checkout (tests/fixtures/profiles)
CAT = catalogue_for("felidae", ["vision"])
P_THR = "senses.vision.detection_threshold"
P_ROD = "senses.vision.measurements.rod_density_peak"
P_POUNCE = "behaviours.pounce.latency_ms"


def _claim(doi: str, path: str, value: float, unit: str, quote: str, **kw: Any) -> extract.Claim:
    raw = {
        "doi": doi,
        "path": path,
        "value": value,
        "value_low": None,
        "value_high": None,
        "unit": unit,
        "quote": quote,
        "section": "Results",
        "conditions": "",
        "species": "Felis catus",
        "source_type": "primary",
        "cites": None,
        "confidence_in_mapping": "exact",
        "_fulltext": True,
    }
    raw.update(kw)
    c, why = extract.check_claim(raw, quote, CAT)
    assert c is not None, why
    return c


KANG_Q = "Cats had roughly 0.74 log unit greater absolute sensitivity than humans"
GUNTER_Q = "the absolute threshold of the cat is about 0.8 log unit below the human"
ROD_Q = "maximum density was about 460,000/mm2 at an eccentricity of 10-15 deg"
ROD2_Q = "peak rod density reached 600000 cells/mm2 in the dorsal retina"
POUNCE_Q = "the strike followed the crouch after a median latency of 350 ms"


def _rec(claims: list[extract.Claim]) -> Any:
    return reconcile(claims, [p.path for p in CAT.paths])


@pytest.fixture
def lib(tmp_path: Path) -> Path:
    d = tmp_path / "library"
    d.mkdir()
    shutil.copy(REPO / "library" / "felidae.yaml", d / "felidae.yaml")
    shutil.copytree(REPO / "library" / "schema", d / "schema")
    return d


# --- assemble -----------------------------------------------------------------------


def test_assemble_new_entry_validates_and_carries_evidence(tmp_path: Path) -> None:
    rec = _rec(
        [
            _claim(DOI_KANG, P_THR, 0.74, "log units", KANG_Q),
            _claim(DOI_GUNTER, P_THR, 0.8, "log units", GUNTER_Q, _fulltext=False),
            _claim(DOI_STEINBERG, P_ROD, 460000, "/mm2", ROD_Q),
            _claim(DOI_KANG, P_POUNCE, 350, "ms", POUNCE_Q),
        ]
    )
    data, log = assemble.assemble(
        None, rec, CAT, clade="Felidae", common_names=["cats"], queries=["q1"], cache_digest="abc"
    )
    assert data["review_status"] == "draft" and data["curated_by"].startswith("senseai-curate")
    assert data["reviewed_by"] is None and data["curation"]["queries"] == ["q1"]
    vision = data["senses"][0]
    assert vision["id"] == "vision" and "TODO" in vision["organ"]
    thr = vision["detection_threshold"]
    # Gunter is abstract-only, so it supports but does not make a second independent primary
    assert thr["confidence"] == "medium" and thr["citations"] == [DOI_KANG, DOI_GUNTER]
    assert [e["doi"] for e in thr["evidence"]] == [DOI_KANG, DOI_GUNTER]
    assert thr["evidence"][0]["quote"] == KANG_Q and thr["evidence"][1]["fulltext"] is False
    assert thr["evidence"][1]["source_type"] == "abstract_only"
    rod = vision["measurements"][0]
    assert (
        rod["id"] == "rod_density_peak" and rod["value"] == 460000 and rod["unit"] == "cells/mm^2"
    )
    assert (
        data["behaviours"][0]["name"] == "pounce"
        and data["behaviours"][0]["latency_ms"]["value"] == 350
    )
    assert set(log.placeholders) == {"senses.vision", "behaviours.pounce"}
    assert set(log.written) == {P_THR, P_ROD, P_POUNCE}
    out = tmp_path / "felidae.yaml"
    assemble.dump_entry(data, out)
    assert out.read_text().startswith("# SPDX-License-Identifier: CC-BY-4.0")
    load_entry(out, schema_path=REPO / "library" / "schema" / "clade.schema.json")


def test_assemble_merges_into_existing_entry(lib: Path) -> None:
    existing = assemble.load_entry_data(lib / "felidae.yaml")
    assert existing is not None
    rec = _rec(
        [
            _claim(DOI_KANG, P_THR, 0.74, "log units", KANG_Q),  # agrees with -0.74 in the entry
            _claim(DOI_KANG, P_ROD, 600000, "cells/mm2", ROD2_Q),  # conflicts with 460000
        ]
    )
    data, log = assemble.assemble(existing, rec, CAT, clade="Felidae")
    vision = next(s for s in data["senses"] if s["id"] == "vision")
    thr = vision["detection_threshold"]
    assert thr["value"] == -0.74 and thr["evidence"][0]["quote"] == KANG_Q  # kept, evidence added
    assert P_THR in log.kept
    rod = next(m for m in vision["measurements"] if m["id"] == "rod_density_peak")
    assert rod["value"] == 600000 and rod["confidence"] == "low"
    assert "Replaced earlier value 460000" in rod["notes"]
    assert log.replaced == [(P_ROD, 460000, 600000.0)]
    assert vision["organ"].startswith("camera eye")  # untouched
    hearing = next(s for s in data["senses"] if s["id"] == "hearing")
    assert "evidence" not in hearing["range"]
    # a second pass over a path with a reviewer note leaves it alone
    rod["reviewer_note"] = "accept"
    data2, log2 = assemble.assemble(data, rec, CAT, clade="Felidae")
    assert P_ROD in log2.skipped
    assert (
        next(m for m in data2["senses"][0]["measurements"] if m["id"] == "rod_density_peak")[
            "value"
        ]
        == 600000
    )


def test_validate_rejects_evidence_doi_missing_from_citations(lib: Path) -> None:
    data = assemble.load_entry_data(lib / "felidae.yaml")
    assert data is not None
    data["senses"][0]["detection_threshold"]["evidence"] = [{"doi": "10.1000/other", "quote": "x"}]
    with pytest.raises(LibraryError, match="not among the value's citations"):
        validate_entry(data)
    data["senses"][0]["detection_threshold"]["evidence"] = [{"doi": DOI_KANG, "quote": "x"}]
    validate_entry(data)


# --- review sheet and approve ---------------------------------------------------------


def _draft(lib: Path) -> tuple[dict[str, Any], review.SheetInput]:
    rec = _rec(
        [
            _claim(DOI_KANG, P_THR, 0.74, "log units", KANG_Q),
            _claim(DOI_GUNTER, P_THR, 0.8, "log units", GUNTER_Q, _fulltext=False),
            _claim(DOI_KANG, P_ROD, 600000, "cells/mm2", ROD2_Q),
        ]
    )
    data, _ = assemble.assemble(None, rec, CAT, clade="Felidae", common_names=["cats"])
    assemble.dump_entry(data, lib / "felidae.yaml")
    info = review.SheetInput(
        clade_key="felidae",
        curated_at=data["curated_at"],
        cache_digest="0123456789abcdef",
        papers_screened=3,
        per_source={"openalex": 3},
        fulltext=1,
        abstract_only=2,
        claims_extracted=3,
        counts={"high": 1, "medium": 1, "low": 0},
        flags_detail={"pubpeer": [f"{DOI_KANG} has 1 thread"]},
        paper_flags={DOI_KANG: ["pubpeer:1"]},
        agreement={P_THR: "agree (±10 %)"},
        empty_paths={"senses.vision.field_of_view_deg": "3 queries, 0 claims"},
        queries=["Felidae eye threshold [openalex: 3]"],
        placeholders=["senses.vision"],
    )
    return data, info


def test_sheet_rows_and_approve_gate(lib: Path) -> None:
    data, info = _draft(lib)
    text = review.render_sheet(data, info)
    sheet = lib / "review" / "felidae.md"
    sheet.parent.mkdir()
    sheet.write_text(text)
    assert "# Review sheet: Felidae (felidae.yaml)" in text
    assert "| 1 | detection_threshold | 0.74 ⚠ | log10 re human | medium | 2 |" in text
    assert f'**{DOI_KANG}** (primary, full text) Results: "{KANG_Q}"' in text
    assert "senses.vision.field_of_view_deg — 3 queries, 0 claims" in text
    assert "Placeholders to fill by hand" in text and "pubpeer" in text
    assert review.warn_rows(text) == {P_THR, P_ROD}  # both carry the pubpeer flag
    with pytest.raises(ValueError, match=f"decision required on: {P_THR}, {P_ROD}"):
        review.approve(lib / "felidae.yaml", sheet, reviewer="Dr Who")
    filled = text.replace(f'"{GUNTER_Q}" |  |', f'"{GUNTER_Q}" | accept: checked against the PDF |')
    filled = filled.replace(
        f'"{ROD2_Q}" |  |', f'"{ROD2_Q}" | accept value=460000 dorsal peak, not ring |'
    )
    sheet.write_text(filled)
    decisions = review.parse_decisions(filled)
    assert (
        decisions[P_THR].verdict == "accept" and decisions[P_THR].note == "checked against the PDF"
    )
    assert decisions[P_ROD].value == "460000"
    out = review.approve(lib / "felidae.yaml", sheet, reviewer="Dr Who", now=None)
    assert out["decisions"] == 2 and out["open_questions"] == []
    entry = yaml.safe_load((lib / "felidae.yaml").read_text())
    assert entry["review_status"] == "reviewed" and entry["reviewed_by"] == "Dr Who"
    assert {"name": "Dr Who", "role": "reviewer"} in entry["contributors"]
    vision = entry["senses"][0]
    assert vision["detection_threshold"]["reviewer_note"] == "accept: checked against the PDF"
    rod = vision["measurements"][0]
    assert rod["value"] == 460000 and rod["reviewer_note"].startswith("accept value=460000")


def test_reject_defer_and_bad_decisions(lib: Path) -> None:
    data, info = _draft(lib)
    text = review.render_sheet(data, info)
    text = text.replace(f'"{GUNTER_Q}" |  |', f'"{GUNTER_Q}" | defer need the full Gunter paper |')
    text = text.replace(f'"{ROD2_Q}" |  |', f'"{ROD2_Q}" | reject wrong retina region |')
    sheet = lib / "felidae.md"
    sheet.write_text(text)
    out = review.approve(lib / "felidae.yaml", sheet, reviewer="R")
    assert len(out["open_questions"]) == 2
    entry = yaml.safe_load((lib / "felidae.yaml").read_text())
    assert entry["senses"][0]["measurements"] == []
    assert entry["senses"][0]["detection_threshold"]["reviewer_note"].startswith("defer")
    assert any("rejected by reviewer" in q for q in entry["open_questions"])
    with pytest.raises(ValueError, match="cannot parse decision"):
        review.parse_decisions(text.replace("| defer need", "| maybe need"))
    assert review._parse_value("26000-27000") == [26000.0, 27000.0]
    assert review._parse_value("-0.74") == -0.74


def test_low_confidence_needs_note_even_without_flag(lib: Path) -> None:
    data, info = _draft(lib)
    data["senses"][0]["measurements"][0]["confidence"] = "low"
    assemble.dump_entry(data, lib / "felidae.yaml")
    text = review.render_sheet(data, review.SheetInput(**{**info.__dict__, "paper_flags": {}}))
    sheet = lib / "felidae.md"
    sheet.write_text(text)
    with pytest.raises(ValueError, match="decision required on: " + P_ROD):
        review.approve(lib / "felidae.yaml", sheet, reviewer="R")


# --- pipeline and CLI, end to end on fixtures ------------------------------------------


def _fake_call(payload: dict[str, Any]) -> dict[str, Any]:
    doc = payload["messages"][0]["content"][0]["source"]["data"]
    claims: list[dict[str, Any]] = []
    if "0.74 log unit" in doc:
        claims.append(
            {
                "path": P_THR,
                "value": 0.74,
                "value_low": None,
                "value_high": None,
                "unit": "log units",
                "quote": KANG_Q,
                "section": "Results",
                "conditions": "scotopic",
                "species": "Felis catus",
                "source_type": "primary",
                "cites": None,
                "confidence_in_mapping": "exact",
            }
        )
        claims.append(
            {**claims[0], "value": 0.9, "quote": "made up 0.9 log unit"}
        )  # must be dropped
    if "one log unit below" in doc:
        claims.append(
            {
                "path": P_THR,
                "value": 1.0,
                "value_low": None,
                "value_high": None,
                "unit": "log units",
                "quote": "the absolute threshold of the cat is about one log unit below the human",
                "section": "abstract",
                "conditions": "",
                "species": "Felis catus",
                "source_type": "primary",
                "cites": None,
                "confidence_in_mapping": "exact",
            }
        )  # "one" is not a digit: value_not_in_quote
    return {
        "stop_reason": "tool_use",
        "tool_input": {
            "doi": "x",
            "claims": claims,
            "species_studied": ["Felis catus"],
            "paper_type": "primary",
        },
    }


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, lib: Path) -> Path:
    data = tmp_path / "data"
    monkeypatch.setenv("SENSEAI_DATA", str(data))
    monkeypatch.delenv("PUBPEER_API_KEY", raising=False)
    monkeypatch.setattr(extract, "anthropic_call", _fake_call)
    monkeypatch.setattr(
        curate_cli,
        "_client",
        lambda args: CachedClient(
            data / "curate-cache",
            offline=False,
            email="t@example.org",
            transport=transport(),
            limiter=RateLimiter({}),
        ),
    )
    return tmp_path


def test_cli_draft_review_approve_end_to_end(
    env: Path, lib: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = str(env)
    (lib / "felidae.yaml").unlink()  # start from nothing: taxonomy decides the header
    assert (
        curate_cli.main(
            ["--root", root, "draft", "felidae", "--senses", "vision", "--max-papers", "10"]
        )
        == 0
    )
    out = capsys.readouterr().out
    summary = json.loads(out[: out.rindex("}") + 1])
    assert summary["papers"] >= 3 and summary["claims"] >= 1 and summary["rejected"] >= 1
    assert summary["values"]["medium"] + summary["values"]["high"] >= 1
    entry = yaml.safe_load((lib / "felidae.yaml").read_text())
    assert entry["clade"] == "Felidae" and entry["review_status"] == "draft"
    assert entry["senses"][0]["detection_threshold"]["evidence"][0]["quote"] == KANG_Q
    assert all(
        "0.9" not in e["quote"] for e in entry["senses"][0]["detection_threshold"]["evidence"]
    )
    assert entry["curation"]["papers_screened"] == summary["papers"]
    sheet = (lib / "review" / "felidae.md").read_text()
    assert "detection_threshold" in sheet and "Queries tried" in sheet
    st = pipeline.RunState.load("felidae")
    assert DOI_RETRACTED not in st.vettings or st.vettings[DOI_RETRACTED]["retracted"]
    assert st.cache_digest
    # individual steps re-run from the saved state
    for cmd in ("search", "fetch", "extract", "vet", "reconcile", "assemble", "review", "status"):
        assert curate_cli.main(["--root", root, cmd, "felidae"]) == 0, cmd
    assert curate_cli.main(["--root", root, "taxonomy", "felidae"]) == 0
    assert curate_cli.main(["--root", root, "status"]) == 0
    capsys.readouterr()
    # approve refuses until ⚠ rows are decided, then flips the entry
    rc = curate_cli.main(["--root", root, "approve", "felidae", "--reviewer", "R"])
    err = capsys.readouterr().err
    if rc == 1:
        assert "refused" in err
        text = (lib / "review" / "felidae.md").read_text()
        text = text.replace(" |  |", " | accept ok |")
        (lib / "review" / "felidae.md").write_text(text)
        assert curate_cli.main(["--root", root, "approve", "felidae", "--reviewer", "R"]) == 0
    assert yaml.safe_load((lib / "felidae.yaml").read_text())["review_status"] == "reviewed"


def test_extract_step_caches_model_output(
    env: Path, lib: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    st = pipeline.RunState("felidae", ["vision"])
    st.texts[DOI_KANG] = {
        "doi": DOI_KANG,
        "text": KANG_FULLTEXT,
        "fulltext": True,
        "source": "europepmc",
        "sections": [],
    }
    n = {"calls": 0}

    def counting(payload: dict[str, Any]) -> dict[str, Any]:
        n["calls"] += 1
        return _fake_call(payload)

    mc = ModelCache(env / "data" / "curate-cache")
    pipeline.step_extract(st, cache=mc, call=counting)
    pipeline.step_extract(st, cache=mc, call=counting)
    assert n["calls"] == 1
    assert len(st.extractions[DOI_KANG]["claims"]) == 1
    assert st.extractions[DOI_KANG]["rejected"][0]["reason"] == "quote_not_found"
    p = st.save()
    assert pipeline.RunState.load("felidae").claims()[0].quote == KANG_Q and p.exists()


def test_profiles_cli_rules_paths_and_reviewed_only(capsys: pytest.CaptureFixture[str]) -> None:
    from senseai_profiles.cli import main as profiles_main

    assert profiles_main(["--root", str(REPO), "rules", "--paths"]) == 0
    out = capsys.readouterr().out
    assert "log10 re human" in out
    assert profiles_main(["--root", str(REPO), "rules"]) == 0
    # every library entry is still a draft, so --reviewed-only has nothing to distil from
    rc = profiles_main(
        ["--root", str(REPO), "distill", "panther", "--reviewed-only", "--out", "/tmp/x"]
    )
    assert rc == 1 and "error: panther" in capsys.readouterr().err
