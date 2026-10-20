# SPDX-License-Identifier: Apache-2.0
"""scripts/container-start.sh (the Railway image's start command), run outside a container.

``pip`` is a no-op stand-in (senseai-profiles is already importable here) and the profiles
"remote" is a local git repository made from the fixture library, so nothing touches the
network. The image itself is built in CI (the ``docker`` job)."""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

from tests.conftest import PROFILES, git

pytestmark = pytest.mark.profiles

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "container-start.sh"


def _port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _shim(path: Path, body: str) -> None:
    path.write_text(f"#!/bin/sh\n{body}\n")
    path.chmod(0o755)


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    _shim(bin_ / "pip", 'echo "pip $*" >> "$(dirname "$0")/pip.log"')
    _shim(bin_ / "senseai-research", f'exec {sys.executable} -m senseai_research.cli "$@"')
    remote = tmp_path / "profiles-src"
    shutil.copytree(PROFILES, remote)
    git("init", "-q", "-b", "main", cwd=remote)
    git("-c", "user.name=t", "-c", "user.email=t@t", "add", "-A", cwd=remote)
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "base", cwd=remote)
    return {
        "PATH": f"{bin_}{os.pathsep}{os.environ['PATH']}",
        "HOME": str(tmp_path),
        "RESEARCH_DATA": str(tmp_path / "data"),
        "SENSEAI_PROFILES_GIT": remote.as_uri(),
        "PORT": str(_port()),
        "PYTHONPATH": os.pathsep.join(p for p in sys.path if p),
    }


def test_refuses_to_start_without_the_proxy_token(env: dict[str, str]) -> None:
    r = subprocess.run(["bash", str(SCRIPT)], env=env, capture_output=True, text=True, timeout=60)
    assert r.returncode == 1 and "RESEARCH_PROXY_TOKEN" in r.stderr


def test_clones_profiles_imports_and_serves(env: dict[str, str], tmp_path: Path) -> None:
    env = env | {"RESEARCH_PROXY_TOKEN": "shared-with-studio"}
    log = tmp_path / "out.log"
    with log.open("w") as out:
        p = subprocess.Popen(["bash", str(SCRIPT)], env=env, stdout=out, stderr=subprocess.STDOUT)
    try:
        base = f"http://127.0.0.1:{env['PORT']}"
        for _ in range(300):
            try:
                if httpx.get(f"{base}/healthz", timeout=1).json() == {"ok": True}:
                    break
            except httpx.HTTPError:
                time.sleep(0.2)
        else:
            pytest.fail(log.read_text())
        text = log.read_text()
        assert "cloning" in text and "importing the library" in text and "serving on" in text
        checkout = Path(env["RESEARCH_DATA"]) / "profiles"
        assert (checkout / "library" / "felidae.yaml").is_file()
        assert "x-access-token" not in (checkout / ".git" / "config").read_text()
        assert "-e" in (tmp_path / "bin" / "pip.log").read_text()
        start = httpx.get(f"{base}/api/research/start", timeout=10).json()
        assert start["least_verified"], "the library was imported into the ledger"
    finally:
        p.terminate()
        p.wait(10)

    # A restart finds the checkout and updates it instead of cloning again.
    again = subprocess.run(
        ["bash", "-c", f'timeout 20 bash "{SCRIPT}"'],
        env=env | {"PORT": str(_port())},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert "updating the profiles checkout" in again.stdout
