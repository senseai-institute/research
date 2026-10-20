from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from senseai_research.curate import extract, reconcile, units, vet
from senseai_research.curate.cache import CachedClient, ModelCache, RateLimiter
from senseai_research.curate.catalogue import catalogue_for
from senseai_research.curate.fetch import PaperText
from senseai_research.curate.search import Paper
from tests.curate.fixtures import (
    DOI_GUNTER,
    DOI_KANG,
    DOI_RETRACTED,
    DOI_REVIEW,
    DOI_STEINBERG,
    KANG_FULLTEXT,
    REVIEW_ABSTRACT,
    STEINBERG_ABSTRACT,
    transport,
)

CAT = catalogue_for("felidae", ["vision"])
P_THR = "senses.vision.detection_threshold"
P_ROD = "senses.vision.measurements.rod_density_peak"


def _raw(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "path": P_THR,
        "value": 0.74,
        "value_low": None,
        "value_high": None,
        "unit": "log units",
        "quote": "Cats had roughly 0.74 log unit greater absolute sensitivity than humans",
        "section": "abstract",
        "conditions": "scotopic",
        "species": "Felis catus",
        "source_type": "primary",
        "cites": None,
        "confidence_in_mapping": "exact",
    }
    base.update(over)
    return base


def _tool(claims: list[dict[str, Any]], paper_type: str = "primary") -> dict[str, Any]:
    return {
        "doi": "x",
        "claims": claims,
        "species_studied": ["Felis catus"],
        "paper_type": paper_type,
    }


KANG = PaperText(DOI_KANG, KANG_FULLTEXT, True, "europepmc")
STEIN = PaperText(DOI_STEINBERG, STEINBERG_ABSTRACT, False, "abstract")
REV = PaperText(DOI_REVIEW, REVIEW_ABSTRACT, False, "abstract")


# --- units ---------------------------------------------------------------------


def test_units_normalise() -> None:
    assert units.normalise_unit("kHz") == ("Hz", 1000.0)
    assert units.normalise_unit("s") == ("ms", 1000.0)
    arcmin = units.normalise_unit("arcmin")
    assert arcmin is not None and arcmin[0] == "deg" and arcmin[1] == pytest.approx(1 / 60)
    assert units.normalise_unit("cells/mm2") == ("cells/mm^2", 1.0)
    assert units.normalise_unit("log units") == ("log10 re human", 1.0)
    assert units.normalise_unit("cycles per degree") == ("cycles/deg", 1.0)
    assert units.normalise_unit("furlongs per fortnight") is None
    assert units.normalise_value(2.0, "cm") == (0.02, "m")


# --- extraction checks -----------------------------------------------------------


def test_quote_must_be_verbatim() -> None:
    ex = extract.verify(_tool([_raw()]), KANG, CAT)
    assert ex.status == "ok" and len(ex.claims) == 1
    c = ex.claims[0]
    assert c.canonical_unit == "log10 re human" and c.canonical_value == 0.74 and not c.flags
    ex = extract.verify(
        _tool([_raw(quote="Cats had about 0.74 log unit greater sensitivity")]), KANG, CAT
    )
    assert ex.status == "no_claims" and ex.rejected[0][1] == "quote_not_found"


def test_value_must_appear_in_quote_and_path_must_exist() -> None:
    ex = extract.verify(_tool([_raw(value=0.8)]), KANG, CAT)
    assert ex.rejected[0][1] == "value_not_in_quote"
    ex = extract.verify(_tool([_raw(path="senses.vision.magic")]), KANG, CAT)
    assert ex.rejected[0][1] == "unknown_path"
    ex = extract.verify(_tool([_raw(value=None)]), KANG, CAT)
    assert ex.rejected[0][1] == "no_value"


def test_ranges_units_and_abstract_only() -> None:
    raw = _raw(
        path="senses.vision.measurements.cone_density_peak",
        value=None,
        value_low=26000,
        value_high=27000,
        unit="/mm2",
        quote="Cone density in the area centralis peaked at 26,000-27,000/mm2",
    )
    ex = extract.verify(_tool([raw]), STEIN, CAT)
    c = ex.claims[0]
    assert c.is_range and (c.canonical_low, c.canonical_high) == (26000.0, 27000.0)
    assert c.source_type == "abstract_only"  # primary claim from abstract-only text is downgraded
    weird = _raw(
        unit="wibbles per flonk",
        quote="Cats had roughly 0.74 log unit greater absolute sensitivity than humans",
    )
    c2 = extract.verify(_tool([weird]), KANG, CAT).claims[0]
    assert "unit_unparsed" in c2.flags and c2.canonical_unit is None
    loose = _raw(confidence_in_mapping="loose")
    assert "loose_mapping" in extract.verify(_tool([loose]), KANG, CAT).claims[0].flags


def test_extract_uses_model_cache_and_handles_refusal(tmp_path: Path) -> None:
    calls: list[dict[str, Any]] = []

    def fake_call(payload: dict[str, Any]) -> dict[str, Any]:
        calls.append(payload)
        assert payload["tools"][0]["strict"] is True
        assert payload["messages"][0]["content"][0]["type"] == "document"
        assert "cache_control" in payload["system"][1]
        return {"stop_reason": "tool_use", "tool_input": _tool([_raw()])}

    mc = ModelCache(tmp_path)
    ex = extract.extract(KANG, CAT, cache=mc, call=fake_call, model="test-model")
    assert ex.status == "ok" and ex.claims[0].doi == DOI_KANG
    ex2 = extract.extract(KANG, CAT, cache=mc, call=fake_call, model="test-model")
    assert ex2.status == "cached" and len(calls) == 1
    assert ex2.raw["curator"].startswith("senseai-curate")

    def refuse(payload: dict[str, Any]) -> dict[str, Any]:
        return {"stop_reason": "refusal", "tool_input": None}

    assert extract.extract(STEIN, CAT, cache=mc, call=refuse).status == "refusal"

    def silent(payload: dict[str, Any]) -> dict[str, Any]:
        return {"stop_reason": "end_turn", "tool_input": None}

    assert extract.extract(REV, CAT, cache=mc, call=silent).status == "no_tool_call"
    assert (
        extract.extract(
            PaperText("10.1/empty", "", False, "none"), CAT, cache=mc, call=silent
        ).status
        == "empty_text"
    )


# --- reconcile -------------------------------------------------------------------


def _claim(
    doi: str,
    value: float,
    *,
    source: str = "primary",
    unit: str = "log units",
    mapping: str = "exact",
    path: str = P_THR,
    fulltext: bool = True,
) -> extract.Claim:
    text = f"the value was {value:g} {unit} in this study"
    raw = _raw(
        path=path,
        value=value,
        unit=unit,
        quote=text,
        source_type=source,
        confidence_in_mapping=mapping,
    )
    c, reason = extract.check_claim({**raw, "doi": doi, "_fulltext": fulltext}, text, CAT)
    assert c is not None, reason
    return c


def test_two_independent_primaries_agree_high() -> None:
    r = reconcile.reconcile_path(P_THR, [_claim(DOI_KANG, 0.74), _claim(DOI_GUNTER, 0.8)])
    assert r is not None and r.confidence == "high" and r.independent == 2
    assert r.value == 0.74 and r.unit == "log10 re human" and r.primary_doi == DOI_KANG
    assert r.citations == [DOI_KANG, DOI_GUNTER] and not r.conflicting


def test_single_primary_is_medium_and_review_does_not_add_independence() -> None:
    chains = {DOI_REVIEW: [DOI_KANG]}
    r = reconcile.reconcile_path(
        P_THR, [_claim(DOI_KANG, 0.74), _claim(DOI_REVIEW, 0.74, source="review")], chains=chains
    )
    assert r is not None and r.confidence == "medium" and r.independent == 1
    assert len(r.supporting) == 2  # the review still shows as supporting evidence
    only_review = reconcile.reconcile_path(P_THR, [_claim(DOI_REVIEW, 0.74, source="review")])
    assert (
        only_review is not None
        and only_review.confidence == "medium"
        and "review" in only_review.note
    )


def test_conflict_is_low_with_both_values() -> None:
    r = reconcile.reconcile_path(P_THR, [_claim(DOI_KANG, 0.74), _claim(DOI_GUNTER, 1.2)])
    assert r is not None and r.confidence == "low" and len(r.conflicting) == 1
    assert "1.2" in r.note and "0.74" in r.note
    # relative tolerance for ordinary units: 460000 vs 470000 agree, vs 600000 do not
    a = _claim(DOI_STEINBERG, 460000, unit="cells/mm2", path=P_ROD)
    assert reconcile.agree(a, _claim(DOI_KANG, 470000, unit="cells/mm2", path=P_ROD))
    assert not reconcile.agree(a, _claim(DOI_KANG, 600000, unit="cells/mm2", path=P_ROD))


def test_reconcile_all_paths_and_counts() -> None:
    rec = reconcile.reconcile([_claim(DOI_KANG, 0.74), _claim(DOI_GUNTER, 0.8)], [P_THR, P_ROD])
    assert set(rec.resolved) == {P_THR} and rec.unresolved_paths == [P_ROD]
    assert rec.counts() == {"high": 1, "medium": 0, "low": 0}


# --- vet -------------------------------------------------------------------------


@pytest.fixture
def client(tmp_path: Path) -> CachedClient:
    return CachedClient(
        tmp_path,
        offline=False,
        email="t@example.org",
        transport=transport(),
        limiter=RateLimiter({}),
    )


def test_vet_flags(client: CachedClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PUBPEER_API_KEY", "k")
    retracted = vet.vet(client, Paper(DOI_RETRACTED, "t", cited_by=2))
    assert retracted.retracted and "retraction notice" in retracted.retraction_note
    assert "retracted" in retracted.flags
    gunter = vet.vet(client, Paper(DOI_GUNTER, "t"))
    assert gunter.pubpeer_threads == 1 and gunter.pubpeer_url and "pubpeer:1" in gunter.flags
    review = vet.vet(client, Paper(DOI_REVIEW, "t", referenced_dois=[]))
    assert review.references == [DOI_KANG, DOI_STEINBERG]
    kang = vet.vet(client, Paper(DOI_KANG, "t"))
    assert kang.supporting_citations == 1 and kang.background_citations == 1
    assert not kang.flags
    monkeypatch.delenv("PUBPEER_API_KEY")
    assert vet.vet(client, Paper(DOI_GUNTER, "t")).pubpeer_threads == 0


def test_vet_predatory_list(
    client: CachedClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SENSEAI_DATA", str(tmp_path / "data"))
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "predatory.txt").write_text("J Test\n")
    v = vet.vet(client, Paper(DOI_KANG, "t", venue="J Test"))
    assert "predatory_venue" in v.flags
