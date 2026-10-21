# SPDX-License-Identifier: Apache-2.0
"""Content-addressed HTTP cache with a per-host rate limiter.

Every request the curator makes goes through :class:`CachedClient`. The cache
key is a SHA-256 over method, URL, sorted query parameters and body, so the
same request always hits the same file under ``$SENSEAI_DATA/curate-cache/http/``.
With ``offline=True`` a cache miss raises :class:`CacheMiss` instead of
touching the network, which is how a draft is reproduced from the cache and
how the tests run.

Responses are stored as JSON (status, a few headers, the body as text, when it
was fetched and from where) so they are diffable and inspectable. Errors
(>= 400) are cached too, so a known-missing paper is not re-requested on every
run; ``retry_errors=True`` re-fetches them.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

DEFAULT_RATE_LIMITS: Mapping[str, float] = {
    # requests per second; polite defaults from each service's guidance
    "api.openalex.org": 8.0,
    "api.semanticscholar.org": 0.9,  # 1 rps unauthenticated
    "eutils.ncbi.nlm.nih.gov": 2.5,  # 3 rps without an API key
    "www.ebi.ac.uk": 5.0,
    "api.crossref.org": 4.0,
    "api.unpaywall.org": 5.0,
    "api.core.ac.uk": 1.0,
    "export.arxiv.org": 0.3,
    "api.biorxiv.org": 2.0,
    "api.gbif.org": 5.0,
    "api.opentreeoflife.org": 2.0,
    "api.checklistbank.org": 2.0,
    "query.wikidata.org": 1.0,
    "api.iucnredlist.org": 1.0,
    "pubpeer.com": 0.5,
}
USER_AGENT = "senseai-library/0.1 (https://github.com/senseai-institute/profiles; mailto:{email})"


class CacheMiss(LookupError):
    """Offline mode and the response is not cached."""


# Query parameters that carry credentials (NCBI and OpenAlex ``api_key``, PubPeer ``key``).
# They are left out of the cache key, so a cached answer is found with or without a key, and
# removed from the URL stored on disk, so keys never reach the cache or its S3 copy.
SECRET_PARAMS = frozenset({"api_key", "key"})


def _public(params: Mapping[str, Any] | None) -> dict[str, Any]:
    return {k: v for k, v in (params or {}).items() if k not in SECRET_PARAMS}


def _redact(url: httpx.URL) -> str:
    for k in SECRET_PARAMS:
        url = url.copy_remove_param(k)
    return str(url)


class RateLimiter:
    """Simple per-host minimum-interval limiter (monotonic clock)."""

    def __init__(self, limits: Mapping[str, float] | None = None, default_rps: float = 2.0) -> None:
        self.limits = dict(DEFAULT_RATE_LIMITS if limits is None else limits)
        self.default_rps = default_rps
        self._last: dict[str, float] = {}

    def wait(self, host: str) -> float:
        rps = self.limits.get(host, self.default_rps)
        interval = 1.0 / rps if rps > 0 else 0.0
        now = time.monotonic()
        wait = max(0.0, self._last.get(host, -1e9) + interval - now)
        if wait > 0:
            time.sleep(wait)
        self._last[host] = time.monotonic()
        return wait


@dataclass(frozen=True, slots=True)
class CachedResponse:
    url: str
    status: int
    text: str
    headers: Mapping[str, str]
    fetched_at: str
    from_cache: bool
    key: str

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    def json(self) -> Any:
        return json.loads(self.text)


def data_root() -> Path:
    return Path(os.environ.get("SENSEAI_DATA", Path.home() / ".senseai")).expanduser()


def cache_key(method: str, url: str, params: Mapping[str, Any] | None, body: str | None) -> str:
    canon = json.dumps(
        {"m": method.upper(), "u": url, "p": sorted(_public(params).items()), "b": body or ""},
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(canon.encode()).hexdigest()


class CachedClient:
    """``get``/``post`` with on-disk caching, rate limiting, retries and an offline switch."""

    def __init__(
        self,
        root: Path | None = None,
        *,
        offline: bool | None = None,
        email: str | None = None,
        limiter: RateLimiter | None = None,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 30.0,
        max_retries: int = 3,
    ) -> None:
        self.root = (root or data_root() / "curate-cache") / "http"
        self.root.mkdir(parents=True, exist_ok=True)
        env_off = os.environ.get("SENSEAI_CURATE_OFFLINE", "") not in ("", "0", "false")
        self.offline = env_off if offline is None else offline
        self.email = email or os.environ.get("SENSEAI_CONTACT_EMAIL", "")
        self.limiter = limiter or RateLimiter()
        self.max_retries = max_retries
        self._client = httpx.Client(
            transport=transport,
            timeout=timeout,
            headers={"User-Agent": USER_AGENT.format(email=self.email or "unset")},
            follow_redirects=True,
        )
        self.touched: list[str] = []  # keys used in this session, for the cache digest

    # -- storage ------------------------------------------------------------------

    def _path(self, key: str) -> Path:
        return self.root / key[:2] / f"{key}.json"

    def _load(self, key: str) -> CachedResponse | None:
        p = self._path(key)
        if not p.exists():
            return None
        d = json.loads(p.read_text())
        return CachedResponse(
            d["url"], d["status"], d["text"], d.get("headers", {}), d["fetched_at"], True, key
        )

    def _store(self, key: str, r: CachedResponse) -> None:
        p = self._path(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            json.dumps(
                {
                    "url": r.url,
                    "status": r.status,
                    "headers": dict(r.headers),
                    "text": r.text,
                    "fetched_at": r.fetched_at,
                },
                ensure_ascii=False,
            )
        )

    def digest(self) -> str:
        """SHA-256 over the keys touched in this session, in order of first use."""
        seen: list[str] = []
        for k in self.touched:
            if k not in seen:
                seen.append(k)
        return hashlib.sha256("\n".join(seen).encode()).hexdigest()

    # -- requests -----------------------------------------------------------------

    def request(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        body: str | None = None,
        headers: Mapping[str, str] | None = None,
        retry_errors: bool = False,
    ) -> CachedResponse:
        key = cache_key(method, url, params, body)
        self.touched.append(key)
        cached = self._load(key)
        if cached is not None and (cached.ok or not retry_errors):
            return cached
        if self.offline:
            raise CacheMiss(f"offline and not cached: {method} {url} {_public(params)}")
        host = httpx.URL(url).host
        last: httpx.Response | None = None
        for attempt in range(self.max_retries + 1):
            self.limiter.wait(host)
            resp = self._client.request(
                method, url, params=dict(params or {}), content=body, headers=dict(headers or {})
            )
            last = resp
            if resp.status_code in (429, 500, 502, 503, 504) and attempt < self.max_retries:
                retry_after = resp.headers.get("Retry-After")
                delay = (
                    float(retry_after) if retry_after and retry_after.isdigit() else 2.0**attempt
                )
                time.sleep(min(delay, 30.0))
                continue
            break
        assert last is not None
        keep = {
            k: v for k, v in last.headers.items() if k.lower() in ("content-type", "etag", "date")
        }
        out = CachedResponse(
            _redact(last.url),
            last.status_code,
            last.text,
            keep,
            time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            False,
            key,
        )
        self._store(key, out)
        return out

    def get(self, url: str, **kw: Any) -> CachedResponse:
        return self.request("GET", url, **kw)

    def post(self, url: str, *, body: str, **kw: Any) -> CachedResponse:
        return self.request("POST", url, body=body, **kw)

    def get_json(self, url: str, **kw: Any) -> Any:
        r = self.get(url, **kw)
        if not r.ok:
            raise httpx.HTTPStatusError(f"{r.status} for {r.url}", request=None, response=None)  # type: ignore[arg-type]
        return r.json()

    def close(self) -> None:
        self._client.close()


# -- model-call cache (same idea, for Anthropic responses) ---------------------


class ModelCache:
    """Caches extraction calls by a content hash of (model, system, tools, document)."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = (root or data_root() / "curate-cache") / "extract"
        self.root.mkdir(parents=True, exist_ok=True)
        self.touched: list[str] = []

    @staticmethod
    def key(payload: Mapping[str, Any]) -> str:
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
        ).hexdigest()

    def get(self, key: str) -> dict[str, Any] | None:
        self.touched.append(key)
        p = self.root / f"{key}.json"
        if not p.exists():
            return None
        out: dict[str, Any] = json.loads(p.read_text())
        return out

    def put(self, key: str, value: Mapping[str, Any]) -> None:
        (self.root / f"{key}.json").write_text(json.dumps(dict(value), indent=1, default=str))


# -- S3 mirror -----------------------------------------------------------------


def sync_s3(local: Path, s3_url: str, *, direction: str) -> int:
    """Mirror the cache directory to or from ``s3://bucket/prefix``. Returns files copied.

    Only new or changed files are copied (by size). Needs the ``aws`` extra.
    """
    import boto3  # aws extra

    if not s3_url.startswith("s3://"):
        raise ValueError("expected s3://bucket/prefix")
    bucket, _, prefix = s3_url[5:].partition("/")
    prefix = prefix.strip("/")
    s3 = boto3.client("s3")
    copied = 0
    if direction == "push":
        remote: dict[str, int] = {}
        for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                remote[obj["Key"]] = int(obj["Size"])
        for p in local.rglob("*.json"):
            key = f"{prefix}/{p.relative_to(local).as_posix()}".strip("/")
            if remote.get(key) != p.stat().st_size:
                s3.upload_file(str(p), bucket, key)
                copied += 1
    elif direction == "pull":
        for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                rel = obj["Key"][len(prefix) :].lstrip("/") if prefix else obj["Key"]
                dest = local / rel
                if dest.exists() and dest.stat().st_size == int(obj["Size"]):
                    continue
                dest.parent.mkdir(parents=True, exist_ok=True)
                s3.download_file(bucket, obj["Key"], str(dest))
                copied += 1
    else:
        raise ValueError("direction must be push or pull")
    return copied


__all__ = [
    "CacheMiss",
    "CachedClient",
    "CachedResponse",
    "ModelCache",
    "RateLimiter",
    "cache_key",
    "data_root",
    "sync_s3",
]
