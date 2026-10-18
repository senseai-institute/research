from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from senseai_research.curate.cache import (
    CachedClient,
    CacheMiss,
    ModelCache,
    RateLimiter,
    cache_key,
)


def _transport(calls: list[str], *, fail_first: bool = False) -> httpx.MockTransport:
    state = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        state["n"] += 1
        if fail_first and state["n"] == 1:
            return httpx.Response(503, text="busy", headers={"Retry-After": "0"})
        if request.url.path.endswith("/missing"):
            return httpx.Response(404, text="nope")
        return httpx.Response(200, json={"q": dict(request.url.params), "n": state["n"]})

    return httpx.MockTransport(handler)


def test_key_is_canonical() -> None:
    a = cache_key("get", "https://x/y", {"b": 1, "a": 2}, None)
    b = cache_key("GET", "https://x/y", {"a": 2, "b": 1}, None)
    assert a == b and len(a) == 64
    assert cache_key("GET", "https://x/y", {"a": 3}, None) != a


def test_cache_hit_skips_network(tmp_path: Path) -> None:
    calls: list[str] = []
    c = CachedClient(tmp_path, offline=False, transport=_transport(calls), limiter=RateLimiter({}))
    r1 = c.get("https://api.example.org/v1", params={"q": "cat"})
    r2 = c.get("https://api.example.org/v1", params={"q": "cat"})
    assert r1.ok and not r1.from_cache and r2.from_cache
    assert r1.json() == r2.json() == {"q": {"q": "cat"}, "n": 1}
    assert len(calls) == 1
    assert c.digest() == c.digest()
    stored = json.loads(next(tmp_path.rglob("*.json")).read_text())
    assert stored["status"] == 200 and stored["url"].endswith("?q=cat")


def test_offline_raises_on_miss_but_serves_hits(tmp_path: Path) -> None:
    calls: list[str] = []
    on = CachedClient(tmp_path, offline=False, transport=_transport(calls), limiter=RateLimiter({}))
    on.get("https://api.example.org/v1", params={"q": "cat"})
    off = CachedClient(tmp_path, offline=True, transport=_transport(calls), limiter=RateLimiter({}))
    assert off.get("https://api.example.org/v1", params={"q": "cat"}).from_cache
    with pytest.raises(CacheMiss, match="offline"):
        off.get("https://api.example.org/v1", params={"q": "dog"})
    assert len(calls) == 1


def test_errors_cached_unless_retry_requested(tmp_path: Path) -> None:
    calls: list[str] = []
    c = CachedClient(tmp_path, offline=False, transport=_transport(calls), limiter=RateLimiter({}))
    assert c.get("https://api.example.org/missing").status == 404
    assert c.get("https://api.example.org/missing").from_cache
    assert len(calls) == 1
    c.get("https://api.example.org/missing", retry_errors=True)
    assert len(calls) == 2
    with pytest.raises(httpx.HTTPStatusError):
        c.get_json("https://api.example.org/missing")


def test_retries_on_503(tmp_path: Path) -> None:
    calls: list[str] = []
    c = CachedClient(
        tmp_path,
        offline=False,
        transport=_transport(calls, fail_first=True),
        limiter=RateLimiter({}),
    )
    r = c.get("https://api.example.org/v1")
    assert r.ok and len(calls) == 2


def test_rate_limiter_spaces_requests() -> None:
    lim = RateLimiter({"h": 1000.0})
    assert lim.wait("h") == 0.0
    waited = lim.wait("h")
    assert 0.0 <= waited <= 0.002


def test_env_offline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SENSEAI_CURATE_OFFLINE", "1")
    c = CachedClient(tmp_path, transport=_transport([]), limiter=RateLimiter({}))
    assert c.offline
    with pytest.raises(CacheMiss):
        c.get("https://api.example.org/v1")


def test_model_cache_round_trip(tmp_path: Path) -> None:
    mc = ModelCache(tmp_path)
    k = ModelCache.key({"model": "m", "doc": "text"})
    assert mc.get(k) is None
    mc.put(k, {"claims": [1, 2]})
    assert mc.get(k) == {"claims": [1, 2]}
    assert ModelCache.key({"doc": "text", "model": "m"}) == k


def test_senseai_library_help_from_senseai_research(capsys: pytest.CaptureFixture[str]) -> None:
    """``senseai-library`` ships with senseai-research now (phase 5)."""
    from senseai_research.curate.cli import main

    with pytest.raises(SystemExit) as e:
        main(["--help"])
    assert e.value.code == 0
    assert "senseai-library" in capsys.readouterr().out
