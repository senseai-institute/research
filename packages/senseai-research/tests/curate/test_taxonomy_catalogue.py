from __future__ import annotations

from pathlib import Path

import pytest

from senseai_research.curate import catalogue, taxonomy
from senseai_research.curate.cache import CachedClient, RateLimiter
from tests.curate.fixtures import transport


@pytest.fixture
def client(tmp_path: Path) -> CachedClient:
    return CachedClient(tmp_path, offline=False, transport=transport(), limiter=RateLimiter({}))


def test_resolve_felidae(client: CachedClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("IUCN_API_TOKEN", "tok")
    t = taxonomy.resolve(client, "Felidae")
    assert t.canonical == "Felidae" and t.rank == "family" and t.gbif_key == 9703
    assert "Felinae" in t.synonyms and "Felidae Fischer de Waldheim, 1817" in t.synonyms
    assert t.common_names[:2] == ["Cats", "cats"] or "cats" in [c.lower() for c in t.common_names]
    assert "felids" in t.common_names
    assert [c.name for c in t.children] == ["Felis catus", "Felis silvestris", "Panthera pardus"]
    assert t.children[0].iucn == "VU" and t.children[0].iucn_url is not None
    assert t.ott_id == 563159 and t.col_id == "6F5GH" and t.wikidata == "Q25265"
    assert not [n for n in t.notes if "differs" in n]
    assert "Felidae" in t.search_names and "Felinae" in t.search_names
    d = t.to_dict()
    assert d["children"][2]["name"] == "Panthera pardus"


def test_resolve_unknown_and_missing_token(
    client: CachedClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("IUCN_API_TOKEN", raising=False)
    t = taxonomy.resolve(client, "Dragonidae")
    assert t.canonical == "Dragonidae" and t.rank == "unknown"
    assert any("no match" in n for n in t.notes)
    assert any("IUCN_API_TOKEN" in n for n in t.notes)


def test_catalogue_orders_consumed_paths_first() -> None:
    cat = catalogue.catalogue_for("felidae", ["vision", "hearing", "vibrissae"])
    paths = [p.path for p in cat.paths]
    assert paths[0] == "behaviours.acoustic_orienting.latency_ms" or paths[0].startswith(
        "senses.vision.detection_threshold"
    )
    consumed = {p.path for p in cat.paths if p.priority == 0}
    assert "senses.vision.detection_threshold" in consumed
    assert "behaviours.acoustic_orienting.latency_ms" in consumed
    assert "senses.vision.measurements.rod_density_peak" in paths
    assert "senses.hearing.range" in paths
    assert len(paths) == len(set(paths))
    spec = cat.by_path()["senses.vision.detection_threshold"]
    assert "log10 re human" in spec.units and "retina" in spec.organ_terms
    assert "senses.vision.detection_threshold" in cat.prompt_text()


def test_rule_paths_lists_units() -> None:
    rp = catalogue.rule_paths()
    assert "log10 re human" in rp["vision_supply_vs_demand"]
    assert "ms" in rp["from_latency"]
