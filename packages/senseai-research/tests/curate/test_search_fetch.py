from __future__ import annotations

from pathlib import Path

import pytest

from senseai_research.curate import fetch, search
from senseai_research.curate.cache import CachedClient, RateLimiter
from senseai_research.curate.catalogue import catalogue_for
from tests.curate.fixtures import DOI_KANG, DOI_RETRACTED, DOI_STEINBERG, KANG_ABSTRACT, transport


@pytest.fixture
def client(tmp_path: Path) -> CachedClient:
    return CachedClient(
        tmp_path,
        offline=False,
        email="t@example.org",
        transport=transport(),
        limiter=RateLimiter({}),
    )


def test_norm_doi() -> None:
    assert search.norm_doi("https://doi.org/10.1152/JN.90641.2008") == DOI_KANG
    assert search.norm_doi("doi:10.1/x") == "10.1/x"
    assert search.norm_doi("") is None and search.norm_doi(None) is None


def test_build_queries_are_specific_first() -> None:
    cat = catalogue_for("felidae", ["vision"])
    spec = cat.by_path()["senses.vision.detection_threshold"]
    qs = search.build_queries(["Felidae", "cat"], spec, max_queries=4)
    assert qs[0] == "Felidae vision absolute threshold"
    assert len(qs) == 4 and len(set(qs)) == 4


def test_each_source_parses(client: CachedClient) -> None:
    oa = search.openalex_search(client, "cat vision absolute threshold")
    assert {p.doi for p in oa} >= {DOI_KANG}
    kang = next(p for p in oa if p.doi == DOI_KANG)
    assert kang.pmcid == "PMC2724355" and kang.abstract == KANG_ABSTRACT and kang.is_oa
    assert kang.venue == "J Test" and kang.cited_by == 80
    s2 = search.s2_search(client, "cat vision")
    assert s2[0].doi == DOI_KANG and s2[0].s2_id == "abc"
    pm = search.pubmed_search(client, "cat vision")
    assert pm[0].doi == DOI_KANG and pm[0].pmid == "19339460" and pm[0].year == 2009
    ep = search.europepmc_search(client, "cat vision")
    assert {p.doi for p in ep} == {DOI_KANG, DOI_STEINBERG}


def test_search_dedupes_and_logs(client: CachedClient) -> None:
    cat = catalogue_for("felidae", ["vision"])
    res = search.search(client, ["Felidae", "cat"], cat, max_queries_per_path=1)
    assert DOI_KANG in res.papers and DOI_STEINBERG in res.papers
    kang = res.papers[DOI_KANG]
    assert len(kang.found_by) >= 3 and "senses.vision.detection_threshold" in kang.paths
    assert kang.pmid == "19339460" and kang.s2_id == "abc"  # merged across sources
    assert all(q.hits >= 0 for q in res.queries) and not res.errors
    assert any(q.source == "openalex" and q.path == "senses.vision.bandwidth" for q in res.queries)
    assert res.by_path("senses.vision.bandwidth")
    assert DOI_RETRACTED in {p.doi for p in res.by_path("senses.vision.bandwidth")}


def test_search_survives_dead_source(client: CachedClient, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*a: object, **k: object) -> list[search.Paper]:
        raise RuntimeError("down")

    monkeypatch.setitem(search.SOURCES, "s2", boom)
    cat = catalogue_for("felidae", ["vision"])
    res = search.search(
        client, ["Felidae"], cat, max_queries_per_path=1, sources=("openalex", "s2")
    )
    assert DOI_KANG in res.papers
    assert any("down" in e for e in res.errors)
    assert any(q.hits == -1 and q.source == "s2" for q in res.queries)


def test_fetch_prefers_europepmc_fulltext(client: CachedClient) -> None:
    p = search.Paper(DOI_KANG, "t", pmcid="PMC2724355", abstract=KANG_ABSTRACT)
    t = fetch.fetch_text(client, p)
    assert t.fulltext and t.source == "europepmc"
    assert "0.74 log units below the human value" in t.text
    assert [s[0] for s in t.sections] == ["abstract", "Results", "Methods"]
    assert t.sections[1][1] < t.text.index("Across the four cats")


def test_fetch_falls_back_to_abstract(client: CachedClient) -> None:
    p = search.Paper(DOI_STEINBERG, "t", abstract="  Rods  were\nmore numerous. ")
    t = fetch.fetch_text(client, p)
    assert not t.fulltext and t.source == "abstract" and t.text == "Rods were more numerous."
    none = fetch.fetch_text(client, search.Paper("10.1/nothing", "t"))
    assert none.source == "none" and none.text == ""


def test_unpaywall_routes_to_europepmc(client: CachedClient) -> None:
    p = search.Paper(DOI_KANG, "t")  # no pmcid: Unpaywall gives the Europe PMC URL
    t = fetch.unpaywall_fulltext(client, p)
    assert t is not None and t.source == "europepmc" and t.fulltext
    assert fetch.unpaywall_fulltext(client, search.Paper(DOI_STEINBERG, "t")) is None


def test_normalise() -> None:
    assert fetch.normalise(" a \n\t b  c ") == "a b c"
