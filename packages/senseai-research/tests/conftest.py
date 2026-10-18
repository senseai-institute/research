# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import socket
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

from senseai_research.config import Settings
from senseai_research.ledger.store import Store
from senseai_research.proposals import github

FIXTURES = Path(__file__).parent / "fixtures"
HAVE_PROFILES = importlib.util.find_spec("senseai_profiles") is not None
PROFILES = FIXTURES / "profiles"


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """No network and no API key in tests (spec §13)."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setenv("SENSEAI_CURATE_OFFLINE", "1")
    real = socket.socket.connect

    def guarded(self: socket.socket, addr: object) -> object:
        host = addr[0] if isinstance(addr, tuple) else str(addr)
        if host not in ("127.0.0.1", "localhost", "::1") and not str(host).startswith("/"):
            raise RuntimeError(f"tests must not use the network (tried {addr})")
        return real(self, addr)  # type: ignore[arg-type]

    monkeypatch.setattr(socket.socket, "connect", guarded)


@pytest.fixture
def profiles_root() -> Path:
    return PROFILES


SEQUENCES = ("zurich_city_03_a", "zurich_city_09_b")


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    data_root = tmp_path / "dsec"
    for name in SEQUENCES:
        (data_root / "test" / name / "images" / "left").mkdir(parents=True)
    return Settings(
        data=tmp_path / "data",
        profiles_repo=PROFILES,
        data_root=data_root,
        bench_cmd=f"{sys.executable} {FIXTURES / 'stub_bench.py'}",
        bench_python=sys.executable,
        stack_dir=tmp_path / "stack",
        world_params=FIXTURES / "world" / "params.py",
    )


@pytest.fixture
def store(settings: Settings) -> Iterator[Store]:
    s = Store(settings.db_path)
    yield s
    s.close()


@pytest.fixture
def imported(store: Store, profiles_root: Path) -> Store:
    from senseai_research.ledger.importers import import_library

    import_library(store, profiles_root)
    return store


def real_profiles() -> Path | None:
    """A full profiles checkout, when the environment provides one (CI does)."""
    p = os.environ.get("SENSEAI_PROFILES_REPO")
    return Path(p) if p and (Path(p) / "library").is_dir() else None


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Tests marked ``profiles`` need senseai-profiles (private); skip them without it."""
    if HAVE_PROFILES:
        return
    skip = pytest.mark.skip(reason="senseai-profiles is not installed (private; see CI notes)")
    for item in items:
        if item.get_closest_marker("profiles"):
            item.add_marker(skip)


KANG_DOI = "10.1152/jn.90641.2008"
KANG_SOURCE = f"src:{KANG_DOI}"
OPTICS_QUOTE = (
    "Expressed in retinal illuminance, after correcting for the larger pupil and ocular "
    "transmission, the cat's scotopic threshold was -0.08 log units re human."
)


def store_kang_text(store: Store) -> None:
    """Put the synthetic Kang fixture into the ledger as fetched full text (as fetch would)."""
    from senseai_profiles.curate.fetch import _xml_text, normalise

    from senseai_research.ledger.models import Source

    text, sections = _xml_text((FIXTURES / "papers" / "PMC2724355.xml").read_text())
    sha = store.store_text(normalise(text), list(sections))
    cur = store.get("source", KANG_SOURCE, Source)
    base = cur or Source(id=KANG_SOURCE, created_by="pipeline:test@0", doi=KANG_DOI)
    store.put_source(
        base.model_copy(
            update={
                "fulltext_available": True,
                "fetched_text_sha256": sha,
                "fetched_from": "europepmc",
            }
        ),
        "pipeline:test@0",
    )


def git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    ).stdout.strip()


@pytest.fixture
def fake_github(
    tmp_path: Path, settings: Settings
) -> tuple[Settings, Path, list[dict[str, object]]]:
    """A profiles git checkout, a bare 'GitHub' remote, and a fake REST API."""
    repo = tmp_path / "profiles-checkout"
    shutil.copytree(settings.profiles_repo or Path(), repo)
    git("init", "-q", "-b", "main", cwd=repo)
    git("-c", "user.name=t", "-c", "user.email=t@t", "add", "-A", cwd=repo)
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "base", cwd=repo)
    remote = tmp_path / "remote.git"
    git("clone", "-q", "--bare", str(repo), str(remote), cwd=tmp_path)
    calls: list[dict[str, object]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(
            {
                "url": str(req.url),
                "auth": req.headers.get("authorization"),
                "json": json.loads(req.content),
            }
        )
        return httpx.Response(
            201, json={"html_url": "https://github.com/senseai-institute/profiles/pull/99"}
        )

    github.transport = httpx.MockTransport(handler)
    cfg = settings.with_(profiles_repo=repo, github_token="tok-123", github_remote=str(remote))
    yield cfg, remote, calls  # type: ignore[misc]
    github.transport = None
