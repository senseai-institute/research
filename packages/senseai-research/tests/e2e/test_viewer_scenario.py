# SPDX-License-Identifier: Apache-2.0
"""Spec §11 in a browser: the real viewer (with its Research page and proxy) in front of the real
research server. Model, bench and GitHub are fakes: recorded responses, the stub bench and a
bare git remote with a mock REST API. Writes the screenshot set to ``docs/screens/``.

Needs a built senseai-viewer with the Research page: set ``SENSEAI_VIEWER_CMD`` to its
``senseai-viewer`` executable (CI checks the viewer out next to this repo). Skipped otherwise.
"""

from __future__ import annotations

import os
import shlex
import shutil
import socket
import subprocess
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn

from senseai_research.agent import Agent
from senseai_research.agent.client import set_client_factory
from senseai_research.config import Settings
from senseai_research.ledger.importers import import_library
from senseai_research.ledger.models import Proposal
from senseai_research.ledger.store import Store
from senseai_research.server.app import create_app
from tests.conftest import FIXTURES
from tests.recorded import RecordedTransport, factory

pytestmark = pytest.mark.profiles

CLAIM = "lib:felidae.senses.vision.detection_threshold"
SCREENS = Path(__file__).resolve().parents[4] / "docs" / "screens"
VIEWER = os.environ.get("SENSEAI_VIEWER_CMD")


def _port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture
def stack(
    fake_github: tuple[Settings, Path, list[dict[str, object]]], tmp_path: Path
) -> Iterator[tuple[str, Store, RecordedTransport, list[dict[str, object]]]]:
    if not VIEWER:
        pytest.skip("set SENSEAI_VIEWER_CMD to a senseai-viewer with the Research page")
    cfg, _remote, calls = fake_github
    shutil.copytree(FIXTURES / "http-cache" / "http", cfg.cache_dir / "curate" / "http")
    store = Store(cfg.db_path)
    import_library(store, cfg.profiles_repo or Path())
    transport = RecordedTransport.from_file(FIXTURES / "recorded" / "scenario_11.json")
    set_client_factory(factory(transport))
    app = create_app(
        cfg,
        store=store,
        agent=Agent(store, cfg, offline=True),
        model_check=lambda: (True, "recorded"),
    )
    rport = _port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=rport, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    vport = _port()
    data = tmp_path / "viewer-data"
    (data / "runs").mkdir(parents=True)
    env = {
        **os.environ,
        "RESEARCH_URL": f"http://127.0.0.1:{rport}",
        "DATA_DIR": str(data),
        "DEMO_RUN": "0",
    }
    viewer = subprocess.Popen(
        [*shlex.split(VIEWER), "serve", "--host", "127.0.0.1", "--port", str(vport)],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.STDOUT,
    )
    base = f"http://127.0.0.1:{vport}"
    for _ in range(200):
        try:
            if httpx.get(f"{base}/api/research-status", timeout=1).json().get("enabled"):
                break
        except (httpx.HTTPError, ValueError):
            pass
        time.sleep(0.1)
    else:
        viewer.kill()
        pytest.fail("the viewer did not start with the Research page enabled")
    yield base, store, transport, calls
    viewer.terminate()
    viewer.wait(10)
    server.should_exit = True
    set_client_factory(None)
    store.close()


def _turn(page: Any, text: str) -> None:
    page.fill(".composer textarea", text)
    page.click(".composer button[type=submit]")
    page.wait_for_function(
        "() => document.querySelector('.composer button').textContent === 'Send'", timeout=120_000
    )


def test_scenario_11_in_the_viewer(
    stack: tuple[str, Store, RecordedTransport, list[dict[str, object]]],
) -> None:
    pw = pytest.importorskip("playwright.sync_api")
    base, store, transport, calls = stack
    SCREENS.mkdir(parents=True, exist_ok=True)
    with pw.sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))

        # 1. Open /research, click "Least-verified values": the threshold is unverified.
        page.goto(f"{base}/research")
        page.get_by_role("button", name="Least-verified values Panther depends on").click()
        row = page.locator(f".start-row[data-claim='{CLAIM}']")
        assert row.locator(".badge").get_attribute("data-status") == "unverified"
        row.click()
        page.wait_for_function(
            "() => document.querySelector('.claim-value')?.textContent === '-0.74 log10 re human'"
        )
        page.screenshot(path=str(SCREENS / "1-least-verified.png"))

        # 2. Where does -0.74 come from? Chips, then the passage in the Source tab.
        _turn(page, "Where does −0.74 come from, and is it the right number for a robot camera?")
        assert page.locator("details.tool-chip").count() == 4
        assert "{" not in page.locator(".chat-log").inner_text()
        assert "most of the advantage is optics" in page.locator(".msg.assistant").last.inner_text()
        page.locator("#tab-source").click()
        assert "retinal illuminance" in page.locator("blockquote.passage mark").inner_text()
        page.screenshot(path=str(SCREENS / "2-passage.png"))

        # 3. Use the optics-removed value: a card; approve with a reason; history keeps -0.74.
        _turn(
            page,
            "For a camera, the optics are the lens, not the cat's pupil. "
            "Use the optics-removed value.",
        )
        card = page.locator(".action-card[data-status='pending']")
        card.locator("textarea.rationale").fill(
            "A robot camera has its own lens, so the optics-removed value applies."
        )
        page.screenshot(path=str(SCREENS / "3-card.png"))
        card.get_by_role("button", name="Approve").click()
        page.wait_for_selector(".action-card[data-status='approved']")
        page.locator("#tab-claim").click()
        page.wait_for_function(
            "() => document.querySelector('.claim-value')?.textContent === '-0.08 log10 re human'"
        )
        assert page.locator("ol.history li").count() == 2
        assert "-0.74" in page.locator("ol.history li").nth(1).inner_text()

        # 4. Impact: the profile changes; the world model does not read this value yet.
        _turn(page, "I approved it. What does that change do to Panther?")
        page.locator("#tab-impact").click()
        assert "panther.frame.gain_lut" in page.locator(".impact-view").inner_text()
        assert "does not reach the world model yet" in page.locator(".world-row").inner_text()
        page.screenshot(path=str(SCREENS / "4-impact.png"))

        # 5. Experiment: progress streams; the table puts the chance rate beside each hit rate.
        _turn(
            page,
            "Run it on two night recordings, zurich_city_03_a and zurich_city_09_b, "
            "with panther_prior and the null profile.",
        )
        page.locator("#tab-experiment").click()
        page.wait_for_selector(".experiment-table")
        table = page.locator(".experiment-table").inner_text()
        assert "50% → 67%" in table and "49% → 64%" in table
        assert page.locator(".msg.job").count() >= 1
        page.screenshot(path=str(SCREENS / "5-experiment.png"))

        # 6. Library change: card, approve, PR on (fake) profiles.
        _turn(page, "Good. Please propose the library change with that experiment.")
        card = page.locator(".action-card").last
        assert card.get_attribute("data-status") == "pending"
        card.locator("textarea.rationale").fill(
            "Checked the passage, the impact and the bench results."
        )
        card.get_by_role("button", name="Approve").click()
        page.wait_for_function(
            "() => [...document.querySelectorAll('.action-card')].pop()"
            ".dataset.status === 'approved'",
            timeout=60_000,
        )
        assert "pull/99" in card.inner_text()
        page.screenshot(path=str(SCREENS / "6-library-pr.png"))
        assert errors == []
        browser.close()

    (prop,) = store.all("proposal", Proposal)
    assert prop.status == "pr_open" and len(calls) == 1
    assert transport.problems == [] and not transport.responses
