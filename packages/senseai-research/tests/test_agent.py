# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from senseai_research.agent import Agent
from senseai_research.agent.client import set_client_factory
from senseai_research.agent.doc import render
from senseai_research.agent.numbers import unsupported_numbers
from senseai_research.agent.tools import TOOLS, api_tools
from senseai_research.config import Settings
from senseai_research.ledger.models import Approval, Claim, PendingAction
from senseai_research.ledger.store import Store
from tests.conftest import HAVE_PROFILES, KANG_SOURCE, OPTICS_QUOTE, PROFILES, store_kang_text
from tests.recorded import RecordedTransport, factory

CLAIM = "lib:felidae.senses.vision.detection_threshold"
USER = "user:senseai"


def msg(content: list[dict[str, Any]], stop: str) -> dict[str, Any]:
    return {
        "content": content,
        "stop_reason": stop,
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }


def text(t: str) -> dict[str, Any]:
    return {"type": "text", "text": t}


def tool(i: int, name: str, inp: dict[str, Any]) -> dict[str, Any]:
    return {"type": "tool_use", "id": f"toolu_{i:02d}", "name": name, "input": inp}


class Run:
    def __init__(self, agent: Agent, store: Store, transport: RecordedTransport) -> None:
        self.agent, self.store, self.transport = agent, store, transport
        self.events: list[tuple[str, dict[str, Any]]] = []
        self.conv = agent.new_conversation(USER)

    def say(self, t: str) -> Any:
        return self.agent.send(self.conv, t, emit=lambda k, d: self.events.append((k, d)))

    def of(self, kind: str) -> list[dict[str, Any]]:
        return [d for k, d in self.events if k == kind]


@pytest.fixture
def run(store: Store, settings: Settings) -> Iterator[Any]:
    if HAVE_PROFILES:
        from senseai_research.ledger.importers import import_library

        import_library(store, PROFILES)
        store_kang_text(store)
    made: list[RecordedTransport] = []

    def start(responses: list[dict[str, Any]]) -> Run:
        t = RecordedTransport(responses)
        made.append(t)
        set_client_factory(factory(t))
        return Run(Agent(store, settings, offline=True), store, t)

    yield start
    set_client_factory(None)
    for t in made:
        assert t.problems == [], t.problems


REVISION = {
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
    "reason": "Optics-removed value for a camera.",
}


@pytest.mark.profiles
def test_n1_write_tool_ends_the_turn_and_changes_nothing(run: Any) -> None:
    r = run(
        [msg([text("I'll propose it."), tool(1, "propose_claim_revision", REVISION)], "tool_use")]
    )
    res = r.say("Use the optics-removed value.")
    assert res.stop == "pending_action" and len(r.transport.requests) == 1
    (card,) = r.of("pending_action")
    assert card["action"]["status"] == "pending"
    assert r.store.must("claim", CLAIM, Claim).revision == 0
    assert r.store.events(type_prefix="claim.revised") == []


@pytest.mark.profiles
def test_n1_a_yes_in_the_chat_is_not_an_approval(run: Any) -> None:
    r = run(
        [
            msg([tool(1, "propose_claim_revision", REVISION)], "tool_use"),
            msg(
                [text("Please use the Approve button on the card; I cannot approve it.")],
                "end_turn",
            ),
        ]
    )
    r.say("Use the optics-removed value.")
    r.say("Yes, approve it.")
    (a,) = r.store.all("action", PendingAction)
    assert a.status == "pending" and r.store.all("approval", Approval) == []
    assert r.store.must("claim", CLAIM, Claim).revision == 0


@pytest.mark.profiles
def test_n2_invented_quote_comes_back_as_a_tool_error(run: Any) -> None:
    bad = json.loads(json.dumps(REVISION))
    bad["changes"]["quote"] = "The cat's threshold was -0.08 log units re human, as everyone knows."
    r = run(
        [
            msg([tool(1, "propose_claim_revision", bad)], "tool_use"),
            msg([text("I could not find that sentence in the paper.")], "end_turn"),
        ]
    )
    res = r.say("Use the optics-removed value.")
    assert res.stop == "end_turn" and r.store.all("action", PendingAction) == []
    tr = r.transport.requests[1]["messages"][-1]["content"][0]
    assert tr["is_error"] is True and "not in the fetched text" in tr["content"]


def test_n5_invented_performance_number_is_flagged(run: Any) -> None:
    r = run([msg([text("With the new value Panther's hit rate is 91% (chance 12%).")], "end_turn")])
    res = r.say("How did it do?")
    assert res.unsupported_numbers == ["91%", "12%"]
    (err,) = [d for d in r.of("error") if d.get("code") == "unverified_number"]
    assert "91%" in err["message"]
    assert r.store.events(type_prefix="agent.unverified_numbers")


def test_n5_numbers_from_the_record_pass() -> None:
    allowed = {0.6667, 0.64, 0.3, 120.0, 24.0}
    ok = "panther_prior: hit rate 67% (chance 64%), median delay 0.30 s, 120 false fires per hour."
    assert unsupported_numbers(ok, allowed) == []
    assert unsupported_numbers("The gain changes at 0.316 lux.", allowed) == []  # not performance
    assert unsupported_numbers("hit rate 70%", allowed) == ["70%"]


def test_refusal_runs_no_tools(run: Any) -> None:
    r = run([msg([tool(1, "claim_get", {"claim_id": CLAIM})], "refusal")])
    res = r.say("Show me the claim.")
    assert res.stop == "refusal" and r.of("tool_call") == []
    assert any(d.get("code") == "refusal" for d in r.of("error"))


def test_max_tokens_never_runs_a_truncated_tool(run: Any) -> None:
    r = run([msg([tool(1, "claim_get", {"claim_id": CLAIM})], "max_tokens")])
    res = r.say("Show me the claim.")
    assert res.stop == "max_tokens" and r.of("tool_call") == []


def test_pause_turn_is_resumed_by_resending(run: Any) -> None:
    r = run([msg([text("Working on it")], "pause_turn"), msg([text(" and done.")], "end_turn")])
    res = r.say("Go.")
    assert res.stop == "end_turn" and len(r.transport.requests) == 2
    last = r.transport.requests[1]["messages"][-1]
    assert last["role"] == "assistant" and last["content"][0]["text"] == "Working on it"


def test_invalid_tool_input_is_returned_as_an_error(run: Any) -> None:
    r = run(
        [
            msg([tool(1, "claim_get", {"claim": CLAIM})], "tool_use"),
            msg([text("Sorry, let me try again.")], "end_turn"),
        ]
    )
    r.say("Show me the claim.")
    tr = r.transport.requests[1]["messages"][-1]["content"][0]
    assert tr["is_error"] is True and "INVALID_JSON" in tr["content"]


@pytest.mark.profiles
def test_tool_failure_is_an_error_result_not_a_crash(run: Any) -> None:
    r = run(
        [
            msg([tool(1, "claim_get", {"claim_id": "lib:nope"})], "tool_use"),
            msg([text("There is no such claim.")], "end_turn"),
        ]
    )
    res = r.say("Show me lib:nope.")
    assert res.stop == "end_turn"
    tr = r.transport.requests[1]["messages"][-1]["content"][0]
    assert tr["is_error"] is True and "no claim" in tr["content"]


@pytest.mark.profiles
def test_parallel_tool_results_go_back_in_one_message(run: Any) -> None:
    r = run(
        [
            msg(
                [
                    tool(1, "claim_get", {"claim_id": CLAIM}),
                    tool(
                        2, "ledger_search", {"query": "rod", "library_path": None, "status": None}
                    ),
                ],
                "tool_use",
            ),
            msg([text("Done.")], "end_turn"),
        ]
    )
    r.say("Look.")
    last = r.transport.requests[1]["messages"][-1]
    assert [b["tool_use_id"] for b in last["content"]] == ["toolu_01", "toolu_02"]


def test_tools_are_strict_sorted_and_complete() -> None:
    tools = api_tools()
    assert [t["name"] for t in tools] == sorted(t.name for t in TOOLS)
    assert {t.name for t in TOOLS} == {
        "ledger_search", "claim_get", "source_passage", "literature_search", "fetch_fulltext",
        "extract_from_source", "impact_preview", "experiment_run", "experiment_get",
        "propose_claim_revision", "propose_new_claim", "propose_hypothesis",
        "propose_library_change",
    }  # fmt: skip
    kinds = {t.name: t.kind for t in TOOLS}
    assert {n for n, k in kinds.items() if k == "write"} == {
        n for n in kinds if n.startswith("propose_")
    }

    def check(schema: dict[str, Any]) -> None:
        if schema.get("type") == "object":
            assert schema["additionalProperties"] is False
            assert sorted(schema["required"]) == sorted(schema["properties"])
            for p in schema["properties"].values():
                check(p)
        for alt in schema.get("anyOf", []):
            check(alt)
        assert "type" not in schema or not isinstance(schema["type"], list)

    for t in tools:
        assert t["strict"] is True and t["eager_input_streaming"] is True
        check(t["input_schema"])
    assert json.dumps(api_tools()) == json.dumps(tools), "byte-stable for caching"


def test_agent_doc_is_generated_from_code() -> None:
    committed = Path(__file__).resolve().parents[3] / "docs" / "AGENT.md"
    assert committed.read_text() == render(), "run `senseai-research agent-doc`"


def test_no_api_key_is_said_plainly(
    store: Store, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    import anthropic

    from senseai_research.agent.client import NO_KEY

    for v in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        monkeypatch.delenv(v, raising=False)
    set_client_factory(lambda: anthropic.Anthropic(api_key=None, max_retries=0))
    try:
        agent = Agent(store, settings, offline=True)
        conv = agent.new_conversation("user:ana")
        events: list[tuple[str, dict[str, Any]]] = []
        r = agent.send(conv, "hello", emit=lambda k, d: events.append((k, d)))
    finally:
        set_client_factory(None)
    assert r.stop == "error"
    assert ("error", {"message": NO_KEY, "code": "no_api_key"}) in events
