# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import os
import socket
from collections.abc import Iterator
from pathlib import Path

import pytest

from senseai_research.config import Settings
from senseai_research.ledger.store import Store

FIXTURES = Path(__file__).parent / "fixtures"
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


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(data=tmp_path / "data", profiles_repo=PROFILES)


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
