# SPDX-License-Identifier: Apache-2.0
"""The settings list of outside services: states, live checks, and that no key value ever leaves
the server or lands in the download cache."""

from __future__ import annotations

import json
import re
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from senseai_research import integrations as svc
from senseai_research.config import Settings
from senseai_research.curate.cache import CachedClient, cache_key
from senseai_research.ledger.store import Store
from senseai_research.server.app import create_app

SECRET = "sk-very-secret-0123456789"
SRC = Path(__file__).resolve().parents[1] / "src" / "senseai_research"


def mock(handler: object) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))  # type: ignore[arg-type]


def test_states() -> None:
    s3 = svc.BY_ID["s3"]
    assert s3.state({}) == "not_set"
    assert s3.state({"RESEARCH_CURATE_S3": "s3://b/p"}) == "partial"
    full = {
        "RESEARCH_CURATE_S3": "s3://b/p",
        "AWS_ACCESS_KEY_ID": "a",
        "AWS_SECRET_ACCESS_KEY": "b",
    }
    assert s3.state(full) == "set"
    assert svc.BY_ID["crossref"].state({}) == "no_key"
    assert svc.BY_ID["unpaywall"].state({"UNPAYWALL_EMAIL": "a@b.org"}) == "set", "alias counts"


def test_status_never_contains_a_value() -> None:
    env = {v.name: f"{SECRET}-{v.name}" for i in svc.INTEGRATIONS for v in i.vars}
    text = json.dumps(svc.status(env))
    assert SECRET not in text
    assert all(r["state"] in ("set", "no_key") for r in svc.status(env))


def test_every_credential_the_code_reads_is_listed() -> None:
    listed = {n for i in svc.INTEGRATIONS for v in i.vars for n in (v.name, *v.aliases)}
    read = set()
    for f in SRC.rglob("*.py"):
        read |= set(re.findall(r'environ\.get\("([A-Z0-9_]*(?:KEY|TOKEN|EMAIL))"', f.read_text()))
    read -= {"RESEARCH_GITHUB_API"}
    assert read and read <= listed, f"add to integrations.INTEGRATIONS: {sorted(read - listed)}"


def test_check_sends_the_key_and_reads_the_answer() -> None:
    seen: list[httpx.Request] = []

    def ok(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(200, json={"results": []})

    r = svc.check("openalex", {"OPENALEX_API_KEY": SECRET}, http=mock(ok))
    assert r == svc.Result(True, "working")
    assert seen[0].url.params["api_key"] == SECRET

    refused = svc.check(
        "openalex", {"OPENALEX_API_KEY": SECRET}, http=mock(lambda q: httpx.Response(403))
    )
    assert not refused.ok and "refused" in refused.detail

    keyless = svc.check("openalex", {}, http=mock(ok))
    assert keyless.ok and "without a key" in keyless.detail
    assert "api_key" not in seen[-1].url.params


def test_check_without_a_needed_key_makes_no_request() -> None:
    def boom(req: httpx.Request) -> httpx.Response:
        raise AssertionError("no request without the key")

    r = svc.check("core", {}, http=mock(boom))
    assert r == svc.Result(False, "not set: CORE_API_KEY")


def test_check_errors_never_echo_the_key() -> None:
    def down(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"failed for {req.url}", request=req)

    r = svc.check("ncbi", {"NCBI_API_KEY": SECRET}, http=mock(down))
    assert not r.ok and SECRET not in r.detail and "ConnectError" in r.detail


def test_github_check_names_the_problem() -> None:
    env = {"GITHUB_TOKEN": SECRET, "RESEARCH_GITHUB_REPO": "o/r"}

    def handler(req: httpx.Request) -> httpx.Response:
        assert req.headers["Authorization"] == f"Bearer {SECRET}"
        return httpx.Response(404)

    r = svc.check("github", env, http=mock(handler))
    assert not r.ok and "cannot see o/r" in r.detail


def test_keys_stay_out_of_the_cache(tmp_path: Path) -> None:
    url = "https://api.openalex.org/works"
    assert cache_key("GET", url, {"q": "x", "api_key": SECRET}, None) == cache_key(
        "GET", url, {"q": "x"}, None
    )
    c = CachedClient(
        tmp_path,
        offline=False,  # the mock transport is local; the suite is offline by default
        transport=httpx.MockTransport(lambda q: httpx.Response(200, text="{}")),
    )
    c.get(url, params={"q": "x", "api_key": SECRET})
    c.get("https://pubpeer.com/api/v2/publications", params={"doi": "d", "key": SECRET})
    stored = "".join(p.read_text() for p in tmp_path.rglob("*.json"))
    assert stored and SECRET not in stored


@pytest.mark.profiles
def test_addresses_match_curate() -> None:
    from senseai_research.curate import fetch, search, taxonomy, vet

    for mod, names in (
        (fetch, ("ARXIV", "CORE", "UNPAYWALL")),
        (search, ("EUROPEPMC", "OPENALEX", "PUBMED", "S2")),
        (taxonomy, ("COL", "GBIF", "IUCN", "WIKIDATA")),
        (vet, ("CROSSREF", "PUBPEER")),
    ):
        for n in names:
            assert getattr(svc, n) == getattr(mod, n), n


@pytest.mark.profiles
def test_openalex_key_is_sent(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("OPENALEX_API_KEY", SECRET)
    from senseai_research.curate.search import openalex_params

    c = CachedClient(tmp_path, email="lab@example.org")
    assert openalex_params(c) == {"mailto": "lab@example.org", "api_key": SECRET}
    monkeypatch.delenv("OPENALEX_API_KEY")
    assert openalex_params(c) == {"mailto": "lab@example.org"}


def test_settings_endpoints(
    settings: Settings, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("IUCN_API_TOKEN", SECRET)
    calls: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(str(req.url.host))
        return httpx.Response(200, json={})

    app = create_app(
        settings,
        store=store,
        trusted_hosts=("testclient",),
        model_check=lambda: (True, "ok"),
        check_http=mock(handler),
    )
    with TestClient(app) as client:
        page = client.get("/api/research/settings")
        assert page.status_code == 200 and SECRET not in page.text
        iucn = next(r for r in page.json()["integrations"] if r["id"] == "iucn")
        assert iucn["state"] == "set" and iucn["vars"][0] == {
            "name": "IUCN_API_TOKEN",
            "aliases": [],
            "secret": True,
            "set": True,
        }
        assert "Railway" in page.json()["where"]
        assert client.post("/api/research/settings/iucn/check").status_code == 401
        user = {"X-SenseAI-User": "alice"}
        first = client.post("/api/research/settings/iucn/check", headers=user).json()
        again = client.post("/api/research/settings/iucn/check", headers=user).json()
        assert first == {"id": "iucn", "ok": True, "detail": "working", "cached": False}
        assert again["cached"] is True and calls == ["api.iucnredlist.org"]
        assert client.post("/api/research/settings/nope/check", headers=user).status_code == 404


def test_api_doc_is_generated_from_code() -> None:
    committed = Path(__file__).resolve().parents[3] / "docs" / "APIS.md"
    assert committed.read_text() == svc.render_doc(), "run `senseai-research api-doc`"
