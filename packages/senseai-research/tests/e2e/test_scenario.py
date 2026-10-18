# SPDX-License-Identifier: Apache-2.0
"""Spec §11, headless: recorded Anthropic responses, the fixture paper, the stub bench and a
fake GitHub. Each numbered step of the scenario is a block below."""

from __future__ import annotations

import shutil
import sys
import traceback
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from senseai_research import views
from senseai_research.agent import Agent
from senseai_research.agent.client import set_client_factory
from senseai_research.config import Settings
from senseai_research.ledger.importers import import_library
from senseai_research.ledger.models import Approval, Claim, Experiment, Impact, Proposal, Source
from senseai_research.ledger.store import Store
from tests.conftest import FIXTURES, OPTICS_QUOTE, git
from tests.recorded import RecordedTransport, factory

pytestmark = pytest.mark.profiles

CLAIM = "lib:felidae.senses.vision.detection_threshold"
USER = "user:senseai"


class Events:
    def __init__(self) -> None:
        self.all: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, kind: str, data: dict[str, Any]) -> None:
        self.all.append((kind, data))

    def of(self, kind: str) -> list[dict[str, Any]]:
        return [d for k, d in self.all if k == kind]

    def text(self) -> str:
        return "".join(d["text"] for d in self.of("text_delta"))


@pytest.fixture
def world(
    fake_github: tuple[Settings, Path, list[dict[str, object]]],
) -> Iterator[tuple[Agent, Store, Settings, RecordedTransport, Path, list[dict[str, object]]]]:
    cfg, remote, calls = fake_github
    shutil.copytree(FIXTURES / "http-cache" / "http", cfg.cache_dir / "curate" / "http")
    store = Store(cfg.db_path)
    import_library(store, cfg.profiles_repo or Path())
    transport = RecordedTransport.from_file(FIXTURES / "recorded" / "scenario_11.json")
    set_client_factory(factory(transport))
    agent = Agent(store, cfg, offline=True)
    yield agent, store, cfg, transport, remote, calls
    set_client_factory(None)
    store.close()


def test_scenario_11(
    world: tuple[Agent, Store, Settings, RecordedTransport, Path, list[dict[str, object]]],
) -> None:
    play(world)


def play(
    world: tuple[Agent, Store, Settings, RecordedTransport, Path, list[dict[str, object]]],
) -> None:
    agent, store, cfg, transport, remote, calls = world
    checkout = cfg.profiles_repo or Path()
    head = git("rev-parse", "HEAD", cwd=checkout)

    # 1. "Least-verified values": the felidae threshold is listed as unverified (abstract only).
    least = views.least_verified(store, cfg)
    dt = next(r for r in least if r["claim_id"] == CLAIM)
    assert least.index(dt) < 3 and dt["status"] == "unverified" and "abstract" in dt["why"]
    assert dt["value"] == "-0.74 log10 re human"

    conv = agent.new_conversation(USER)

    # 2. Where does -0.74 come from? claim_get, fetch_fulltext, then the passages.
    ev = Events()
    r = agent.send(
        conv,
        "Where does −0.74 come from, and is it the right number for a robot camera?",
        emit=ev,
        focus={"claim_id": CLAIM},
    )
    assert r.stop == "end_turn"
    assert [d["name"] for d in ev.of("tool_call")] == [
        "claim_get",
        "fetch_fulltext",
        "source_passage",
        "source_passage",
    ]
    assert all(d["ok"] for d in ev.of("tool_result_summary"))
    assert ev.of("thinking_summary"), "thinking summaries stream to the page"
    assert "most of the advantage is optics" in ev.text() and OPTICS_QUOTE in ev.text()
    src = store.must("source", "src:10.1152/jn.90641.2008", Source)
    assert src.fulltext_available and src.fetched_from == "europepmc"

    # 3. "Use the optics-removed value": a card appears; the claim does not change until approved.
    ev = Events()
    r = agent.send(
        conv,
        "For a camera, the optics are the lens, not the cat's pupil. Use the optics-removed value.",
        emit=ev,
    )
    assert r.stop == "pending_action"
    (card,) = ev.of("pending_action")
    action = card["action"]
    assert action["kind"] == "claim_revision" and action["card"]["quote"] == OPTICS_QUOTE
    assert store.must("claim", CLAIM, Claim).revision == 0
    out = agent.decide(
        action["id"],
        "approve",
        user=USER,
        rationale="A robot camera has a lens, not a pupil: the optics-removed value applies.",
    )
    c = store.must("claim", CLAIM, Claim)
    assert c.revision == 1 and c.quantity.value == -0.08 and c.status == "extracted"
    (rev,) = store.claim_history(CLAIM)
    assert next(ch for ch in rev.changes if ch.field == "quantity").old["value"] == -0.74
    view = agent.view(conv)
    assert any(v["kind"] == "note" and "approved" in v["text"] for v in view)
    assert out.approval.by == USER

    # 4. impact_preview: gain LUT and attention change; the world prior does not.
    ev = Events()
    r = agent.send(conv, "I approved it. What does that change do to Panther?", emit=ev)
    assert r.stop == "end_turn"
    (imp,) = store.all("impact", Impact)
    assert imp.consumers == [
        "panther.event.attention (vision_deficit_boost)",
        "panther.frame.gain_lut (vision_supply_vs_demand)",
        "panther_vision.event.attention (vision_deficit_boost)",
        "panther_vision.frame.gain_lut (vision_supply_vs_demand)",
    ]
    assert not imp.world_reached and "does not reach the world model yet" in imp.world_note

    # 5. experiment_run on two night sequences + the cached baseline; progress streams.
    ev = Events()
    r = agent.send(
        conv,
        "Run it on two night recordings, zurich_city_03_a and zurich_city_09_b, with "
        "panther_prior and the null profile.",
        emit=ev,
    )
    assert r.stop == "end_turn", ev.of("error")
    assert ev.of("job_progress"), "experiment progress streams to the page"
    pat = next(e for e in store.all("experiment", Experiment) if not e.is_baseline)
    assert pat.status == "done" and pat.baseline_experiment_id
    for arm in ("panther_prior", "null_profile"):
        assert pat.outputs[arm].chance_hit_rate is not None
    assert r.unsupported_numbers == [], "every number in the reply is in the stored record (N5)"
    assert not [d for d in ev.of("error") if d.get("code") == "unverified_number"]

    # 6. propose_library_change -> approve -> PR on (fake) profiles with the §7.4 body.
    ev = Events()
    r = agent.send(conv, "Good. Please propose the library change with that experiment.", emit=ev)
    assert r.stop == "pending_action"
    (card,) = ev.of("pending_action")
    out = agent.decide(
        card["action"]["id"], "approve", user=USER, rationale="Checked the passage and the bench."
    )
    assert out.action.status == "approved", out.action.error
    (prop,) = store.all("proposal", Proposal)
    assert prop.status == "pr_open" and prop.pr_url
    body = calls[0]["json"]
    assert isinstance(body, dict)
    assert body["title"] == "library(felidae): senses.vision.detection_threshold -0.74 → -0.08"
    text = str(body["body"])
    for needed in (
        OPTICS_QUOTE,
        "10.1152/jn.90641.2008",
        "Checked the passage and the bench.",
        "panther.frame.gain_lut",
        "chance",
        "replay-compare",
        "Approved by senseai",
    ):
        assert needed in text, needed
    assert "value: -0.08" in git("show", f"research/{prop.id}:library/felidae.yaml", cwd=remote)

    # The model call matched the spec on every request; history stayed append-only.
    assert transport.problems == []
    assert not transport.responses, "every recorded response was used"
    # N1: the profiles checkout itself was never written.
    assert git("rev-parse", "HEAD", cwd=checkout) == head
    assert git("status", "--porcelain", cwd=checkout) == ""


# -- §13 property: nothing writes profiles except proposals/github.py, after an Approval --------

_WRITE_EVENTS = {"os.remove", "os.rename", "os.replace", "os.mkdir", "os.rmdir", "os.truncate"}
_hook: dict[str, Any] = {"on": None}


def _audit(event: str, args: tuple[Any, ...]) -> None:
    sink = _hook["on"]
    if sink is not None and not sink.busy:
        sink.busy = True
        try:
            sink.seen(event, args)
        finally:
            sink.busy = False


class ProfilesWrites:
    """Every write into the profiles checkout or its remote, and every git command that could
    write either, with the senseai_research module that caused it and the approvals at that time."""

    def __init__(self, checkout: Path, remote: Path, store: Store) -> None:
        self.roots = (checkout.resolve(), remote.resolve())
        self.store = store
        self.busy = False
        self.writes: list[tuple[str, str]] = []
        self.pushes: list[tuple[str, int]] = []

    def _inside(self, path: Any) -> bool:
        try:
            p = Path(path if isinstance(path, str | Path) else "").resolve()
        except (OSError, TypeError, ValueError):
            return False
        return any(p == r or r in p.parents for r in self.roots)

    @staticmethod
    def _caller() -> str:
        for f in reversed(traceback.extract_stack()):
            if "senseai_research" in f.filename and "/tests/" not in f.filename:
                return f.filename.split("senseai_research/")[-1]
        return "?"

    def seen(self, event: str, args: tuple[Any, ...]) -> None:
        if event == "open":
            path, mode = args[0], args[1]
            writing = isinstance(mode, str) and any(c in mode for c in "wax+")
            if isinstance(mode, int):
                writing = bool(mode & 0o3)  # os.open flags: O_WRONLY | O_RDWR
            if writing and self._inside(path):
                self.writes.append((self._caller(), str(path)))
        elif event in _WRITE_EVENTS and args and self._inside(args[0]):
            self.writes.append((self._caller(), f"{event} {args[0]}"))
        elif event == "subprocess.Popen":
            argv = [str(a) for a in (args[1] or [])]
            cwd = args[2]
            if "git" in Path(argv[0]).name if argv else False:
                if "push" in argv:
                    approvals = [
                        a
                        for a in self.store.all("approval", Approval)
                        if a.target_kind == "proposal" and a.decision == "approve"
                    ]
                    self.pushes.append((self._caller(), len(approvals)))
                elif (
                    cwd is not None
                    and self._inside(cwd)
                    and argv[1:2]
                    not in (
                        ["rev-parse"],
                        ["log"],
                        ["show"],
                        ["status"],
                        ["ls-files"],
                        ["diff"],
                        ["cat-file"],
                    )
                ):
                    self.writes.append((self._caller(), " ".join(argv)))


sys.addaudithook(_audit)


def test_n1_only_github_writes_profiles_and_only_after_an_approval(
    world: tuple[Agent, Store, Settings, RecordedTransport, Path, list[dict[str, object]]],
) -> None:
    """Spec §13: the whole §11 scenario, watched by a Python audit hook. No code path writes the
    profiles checkout or its remote except ``proposals/github.py``, and that pushes only once an
    Approval row exists for the proposal."""
    _, store, cfg, _, remote, calls = world
    watch = ProfilesWrites(cfg.profiles_repo or Path(), remote, store)
    _hook["on"] = watch
    try:
        play(world)
    finally:
        _hook["on"] = None
    assert watch.writes == [], watch.writes
    assert watch.pushes == [("proposals/github.py", 1)], watch.pushes
    assert len(calls) == 1, "one PR opened, after the push"
