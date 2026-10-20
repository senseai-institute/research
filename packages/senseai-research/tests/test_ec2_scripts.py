# SPDX-License-Identifier: Apache-2.0
"""scripts/install_research_ec2.sh and research-ctl.sh, in a sandbox HOME shaped like the EC2 box
(``~/senseai/.venv``, ``~/senseai/profiles``, ``~/senseai/viewer.env``). Package installation is
skipped (``RESEARCH_SKIP_PIP=1``); the sandbox venv runs this test's own Python. With
``SENSEAI_VIEWER_CMD`` set, the viewer is started too and ``/research`` is checked behind the
viewer password."""

from __future__ import annotations

import base64
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

from tests.conftest import PROFILES

pytestmark = pytest.mark.profiles

SCRIPTS = Path(__file__).resolve().parents[3] / "scripts"


def _port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _wrapper(path: Path, target: str) -> None:
    path.write_text(f'#!/bin/sh\nexec {target} "$@"\n')
    path.chmod(0o755)


@pytest.fixture
def box(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    home = tmp_path / "home"
    stack = home / "senseai"
    (stack / ".venv" / "bin").mkdir(parents=True)
    _wrapper(stack / ".venv" / "bin" / "python", sys.executable)
    _wrapper(
        stack / ".venv" / "bin" / "senseai-research", f"{sys.executable} -m senseai_research.cli"
    )
    shutil.copytree(PROFILES, stack / "profiles")
    (stack / "viewer.env").write_text(f"SENSEAI_DATA_ROOT={tmp_path / 'dsec'}\nSENSEAI_RUNNER=1\n")
    env = {
        "HOME": str(home),
        "PATH": os.environ["PATH"],
        "RESEARCH_SKIP_PIP": "1",
        "RESEARCH_PORT": str(_port()),
        "PYTHONPATH": os.pathsep.join(p for p in sys.path if p),
    }
    return home, env


def _run(args: list[str], env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, env=env, capture_output=True, text=True, timeout=120)


def test_scripts_parse() -> None:
    for s in ("install_research_ec2.sh", "research-ctl.sh"):
        assert subprocess.run(["bash", "-n", str(SCRIPTS / s)]).returncode == 0


def test_install_then_start_status_stop(box: tuple[Path, dict[str, str]]) -> None:
    home, env = box
    stack = home / "senseai"
    r = _run(["bash", str(SCRIPTS / "install_research_ec2.sh")], env)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "==> done" in r.stdout
    renv = (stack / "research.env").read_text()
    assert f"RESEARCH_PORT={env['RESEARCH_PORT']}" in renv and "SENSEAI_PROFILES_REPO=" in renv
    assert (
        f"RESEARCH_URL=http://127.0.0.1:{env['RESEARCH_PORT']}"
        in (stack / "viewer.env").read_text()
    )
    db = sqlite3.connect(stack / "research" / "ledger.sqlite")
    (n,) = db.execute("SELECT count(*) FROM entities WHERE kind='claim'").fetchone()
    assert n > 10
    again = _run(["bash", str(SCRIPTS / "install_research_ec2.sh")], env)
    assert again.returncode == 0 and "already sets RESEARCH_URL" in again.stdout
    assert (stack / "viewer.env").read_text().count("RESEARCH_URL") == 1

    ctl = str(home / ".local" / "bin" / "research-ctl.sh")
    try:
        start = _run([ctl, "start"], env)
        assert start.returncode == 0, start.stdout + start.stderr + _log(stack)
        status = _run([ctl, "status"], env)
        assert status.returncode == 0 and "running, version" in status.stdout
        assert "recordings:" in status.stdout and "dsec" in status.stdout
        port = env["RESEARCH_PORT"]
        s = httpx.get(f"http://127.0.0.1:{port}/api/research/start", timeout=10).json()
        assert s["least_verified"]
        viewer_cmd = os.environ.get("SENSEAI_VIEWER_CMD")
        if viewer_cmd:
            _check_viewer(viewer_cmd, stack, home)
    finally:
        stop = _run([ctl, "stop"], env)
    assert stop.returncode == 0 and "stopped" in stop.stdout
    assert _run([ctl, "status"], env).returncode == 3


def _log(stack: Path) -> str:
    log = stack / "research" / "logs" / "server.log"
    return log.read_text()[-2000:] if log.exists() else ""


def _check_viewer(cmd: str, stack: Path, home: Path) -> None:
    """The viewer, started with the box's viewer.env and a password, serves /research behind it."""
    env = dict(os.environ)
    for line in (stack / "viewer.env").read_text().splitlines():
        k, _, v = line.partition("=")
        env[k] = v
    port = _port()
    env.update(VIEWER_PASSWORD="pw", DATA_DIR=str(home / "viewer-data"), DEMO_RUN="0")
    p = subprocess.Popen(
        [*cmd.split(), "serve", "--port", str(port)],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    auth = {"Authorization": "Basic " + base64.b64encode(b"biologist:pw").decode()}
    try:
        base = f"http://127.0.0.1:{port}"
        for _ in range(100):
            try:
                httpx.get(f"{base}/healthz", timeout=1)
                break
            except httpx.HTTPError:
                time.sleep(0.1)
        assert httpx.get(f"{base}/research").status_code == 401
        assert httpx.get(f"{base}/research", headers=auth).status_code == 200
        assert httpx.get(f"{base}/api/research-status", headers=auth).json() == {"enabled": True}
        r = httpx.get(f"{base}/api/research/start", headers=auth, timeout=10)
        assert r.status_code == 200 and r.json()["least_verified"]
    finally:
        p.terminate()
        p.wait(10)
