# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import argparse
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from senseai_research.agent import Agent
from senseai_research.agent.client import set_client_factory
from senseai_research.config import Settings
from senseai_research.ledger.models import Approval, Claim
from senseai_research.ledger.store import Store
from senseai_research.server.app import create_app
from tests.conftest import HAVE_PROFILES, KANG_SOURCE, OPTICS_QUOTE, PROFILES, store_kang_text
from tests.recorded import RecordedTransport, factory

CLAIM = "lib:felidae.senses.vision.detection_threshold"
ALICE = {"X-SenseAI-User": "alice"}


def sse(text: str) -> list[tuple[str, dict[str, Any]]]:
    out = []
    for block in text.replace("\r\n", "\n").split("\n\n"):
        ev, data = None, None
        for line in block.splitlines():
            if line.startswith("event:"):
                ev = line[6:].strip()
            elif line.startswith("data:"):
                data = line[5:].strip()
        if ev and data is not None:
            out.append((ev, json.loads(data)))
    return out


@pytest.fixture
def server(store: Store, settings: Settings) -> Iterator[tuple[TestClient, Store, Any]]:
    if HAVE_PROFILES:
        from senseai_research.ledger.importers import import_library

        import_library(store, PROFILES)
        store_kang_text(store)
    holder: dict[str, RecordedTransport] = {}

    def use(responses: list[dict[str, Any]]) -> RecordedTransport:
        holder["t"] = RecordedTransport(responses)
        set_client_factory(factory(holder["t"]))
        return holder["t"]

    app = create_app(
        settings,
        store=store,
        agent=Agent(store, settings, offline=True),
        trusted_hosts=("testclient",),
        model_check=lambda: (True, "reachable"),
    )
    with TestClient(app) as client:
        yield client, store, use
    set_client_factory(None)
    if "t" in holder:
        assert holder["t"].problems == []


def test_refuses_anything_but_loopback(settings: Settings, store: Store) -> None:
    app = create_app(settings, store=store, model_check=lambda: (False, "x"))
    with TestClient(app) as client:
        r = client.get("/api/research/health")
        assert client.get("/healthz").json() == {"ok": True}, "open for the platform"
    assert r.status_code == 403 and "RESEARCH_PROXY_TOKEN" in r.json()["detail"]


def test_studio_over_the_network_needs_the_shared_token(settings: Settings, store: Store) -> None:
    cfg = settings.with_(proxy_token="t0k3n-shared-with-studio")
    app = create_app(cfg, store=store, model_check=lambda: (True, "ok"))
    with TestClient(app) as client:  # the test client is not loopback
        assert client.get("/api/research/health").status_code == 403
        wrong = {"X-SenseAI-Proxy-Token": "guess"}
        assert client.get("/api/research/health", headers=wrong).status_code == 403
        ok = {"X-SenseAI-Proxy-Token": "t0k3n-shared-with-studio", "X-SenseAI-User": "alice"}
        assert client.get("/api/research/health", headers=ok).status_code == 200
        assert client.post("/api/research/conversations", headers=ok).status_code == 200


def test_serve_refuses_a_public_address_without_the_token(settings: Settings) -> None:
    from senseai_research import cli

    assert cli.cmd_serve(argparse.Namespace(port=None, host="0.0.0.0"), settings) == 2


def test_health(server: tuple[TestClient, Store, Any]) -> None:
    client, _, _ = server
    h = client.get("/api/research/health").json()
    assert h["model_reachable"] is True and h["model"] == "claude-opus-5-5"
    assert h["bench_cmd"].endswith("stub_bench.py")


def test_user_header_is_required_for_writes(server: tuple[TestClient, Store, Any]) -> None:
    client, _, _ = server
    assert client.post("/api/research/conversations").status_code == 401
    cid = client.post("/api/research/conversations", headers=ALICE).json()["id"]
    r = client.post(f"/api/research/conversations/{cid}/messages", json={"text": "hi"})
    assert r.status_code == 401


def test_message_streams_sse_events(server: tuple[TestClient, Store, Any]) -> None:
    client, _, use = server
    use(
        [
            {
                "content": [
                    {"type": "thinking", "thinking": "Look it up.", "signature": "s"},
                    {"type": "text", "text": "Let me check the ledger."},
                    {
                        "type": "tool_use",
                        "id": "toolu_01",
                        "name": "ledger_search",
                        "input": {"query": "rod", "library_path": None, "status": None},
                    },
                ],
                "stop_reason": "tool_use",
            },
            {"content": [{"type": "text", "text": "Found it."}], "stop_reason": "end_turn"},
        ]
    )
    cid = client.post("/api/research/conversations", headers=ALICE).json()["id"]
    r = client.post(
        f"/api/research/conversations/{cid}/messages", json={"text": "rods?"}, headers=ALICE
    )
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
    events = sse(r.text)
    kinds = [k for k, _ in events]
    assert kinds[0] == "thinking_summary" and "text_delta" in kinds
    assert kinds.index("tool_call") < kinds.index("tool_result_summary") < len(kinds) - 1
    assert kinds[-1] == "done"
    chip = next(d for k, d in events if k == "tool_call")
    assert chip["label"] == "Searching the ledger for “rod”"
    items = client.get(f"/api/research/conversations/{cid}").json()["items"]
    assert [i["kind"] for i in items] == [
        "user",
        "thinking",
        "assistant",
        "tool",
        "tool_result",
        "assistant",
    ]


@pytest.mark.profiles
def test_card_then_approve_through_the_api(server: tuple[TestClient, Store, Any]) -> None:
    client, store, use = server
    use(
        [
            {
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_01",
                        "name": "propose_claim_revision",
                        "input": {
                            "claim_id": CLAIM,
                            "changes": {
                                "value": -0.08,
                                "low": None,
                                "high": None,
                                "unit": "log10 re human",
                                "conditions": None,
                                "species": None,
                                "method": None,
                                "mapping": "close",
                                "status": "extracted",
                                "source_id": KANG_SOURCE,
                                "quote": OPTICS_QUOTE,
                            },
                            "reason": "Optics removed.",
                        },
                    }
                ],
                "stop_reason": "tool_use",
            }
        ]
    )
    cid = client.post("/api/research/conversations", headers=ALICE).json()["id"]
    events = sse(
        client.post(
            f"/api/research/conversations/{cid}/messages",
            json={"text": "Use the optics-removed value.", "focus": {"claim_id": CLAIM}},
            headers=ALICE,
        ).text
    )
    card = next(d for k, d in events if k == "pending_action")["action"]
    assert events[-1][1]["stop"] == "pending_action"
    url = f"/api/research/actions/{card['id']}/approve"
    assert (
        client.post(url, json={"rationale": "A camera has a lens, not a pupil."}).status_code == 401
    )
    r = client.post(url, json={"rationale": "A camera has a lens, not a pupil."}, headers=ALICE)
    assert r.status_code == 200 and r.json()["approval"]["by"] == "user:alice"
    (ap,) = store.all("approval", Approval)
    assert ap.by == "user:alice"
    assert store.must("claim", CLAIM, Claim).quantity.value == -0.08
    again = client.post(url, json={"rationale": "Twice."}, headers=ALICE)
    assert again.status_code == 400 and "already approved" in again.json()["detail"]
    items = client.get(f"/api/research/conversations/{cid}").json()["items"]
    assert any(i["kind"] == "note" and "alice approved" in i["text"] for i in items)
    card_view = client.get(f"/api/research/claims/{CLAIM}").json()
    assert [h["revision"] for h in card_view["history"]] == [0, 1]
    assert "-0.74 log10 re human → -0.08 log10 re human" in card_view["history"][1]["summary"]
    assert card_view["passage"]["quote"] == OPTICS_QUOTE
    assert card_view["passage"]["source"]["link"] == "https://doi.org/10.1152/jn.90641.2008"


@pytest.mark.profiles
def test_start_screen_lists(server: tuple[TestClient, Store, Any]) -> None:
    client, _, _ = server
    s = client.get("/api/research/start").json()
    assert s["least_verified"][0]["status"] == "unverified"
    assert any(r["claim_id"] == CLAIM for r in s["least_verified"][:3])
    assert s["open_questions"][0]["priority"] == "critical"


def test_health_checks_the_model_on_a_freshly_booted_machine(
    settings: Settings, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    import senseai_research.server.app as app_mod

    monkeypatch.setattr(app_mod.time, "monotonic", lambda: 5.0)  # seconds since boot
    app = create_app(
        settings, store=store, trusted_hosts=("testclient",), model_check=lambda: (True, "ok")
    )
    with TestClient(app) as client:
        assert client.get("/api/research/health").json()["model_reachable"] is True


def test_contract_lists_every_event_the_code_emits() -> None:
    import re

    from senseai_research.server import contract

    src = Path(__file__).resolve().parents[1] / "src" / "senseai_research"
    emitted = set()
    for f in src.rglob("*.py"):
        text = f.read_text()
        emitted |= set(re.findall(r'emit\(\s*"([a-z_]+)"', text))
        emitted |= set(re.findall(r'q\.put\(\(\s*"([a-z_]+)"', text))
    assert emitted and emitted <= set(contract.EVENTS), sorted(emitted - set(contract.EVENTS))


def test_api_doc_is_generated_from_code(settings: Settings, store: Store) -> None:
    from senseai_research.server import contract

    app = create_app(settings, store=store, model_check=lambda: (True, "ok"))
    committed = Path(__file__).resolve().parents[3] / "docs" / "API.md"
    assert committed.read_text() == contract.render_doc(app), "run `senseai-research api-doc`"
    assert all(d for _, _, d in contract.routes(app)), "every route needs a one-line docstring"
