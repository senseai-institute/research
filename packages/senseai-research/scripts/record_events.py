# SPDX-License-Identifier: Apache-2.0
"""Record the research service's real chat events and transcript for Studio's adapter tests.

Runs turns 2-3 of the §11 scenario with the recorded model responses (no key, no network) and
writes what a front end receives. Studio keeps the output at
``web/src/research/fixtures/research_events.json``; re-run this when a chat payload changes::

    SENSEAI_CURATE_OFFLINE=1 python scripts/record_events.py \\
        <studio>/web/src/research/fixtures/research_events.json

Needs senseai-profiles (the test fixtures import it).
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

PKG = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PKG))

from senseai_research.agent import Agent  # noqa: E402
from senseai_research.agent.client import set_client_factory  # noqa: E402
from senseai_research.config import Settings  # noqa: E402
from senseai_research.ledger.importers import import_library  # noqa: E402
from senseai_research.ledger.store import Store  # noqa: E402
from tests.conftest import FIXTURES, PROFILES, SEQUENCES  # noqa: E402
from tests.recorded import RecordedTransport, factory  # noqa: E402

CLAIM = "lib:felidae.senses.vision.detection_threshold"
TURNS = (
    "Where does −0.74 come from, and is it the right number for a robot camera?",
    "For a camera, the optics are the lens, not the cat's pupil. Use the optics-removed value.",
)


def main(out: Path) -> None:
    tmp = Path(tempfile.mkdtemp())
    data_root = tmp / "dsec"
    for n in SEQUENCES:
        (data_root / "test" / n / "images" / "left").mkdir(parents=True)
    cfg = Settings(
        data=tmp / "data",
        profiles_repo=PROFILES,
        data_root=data_root,
        bench_cmd=f"{sys.executable} {FIXTURES / 'stub_bench.py'}",
        bench_python=sys.executable,
        stack_dir=tmp / "stack",
        world_params=FIXTURES / "world" / "params.py",
    )
    shutil.copytree(FIXTURES / "http-cache" / "http", cfg.cache_dir / "curate" / "http")
    store = Store(cfg.db_path)
    import_library(store, PROFILES)
    recorded = RecordedTransport.from_file(FIXTURES / "recorded" / "scenario_11.json")
    set_client_factory(factory(recorded))
    agent = Agent(store, cfg, offline=True)
    conv = agent.new_conversation("user:senseai")
    events: list[dict[str, Any]] = []

    def emit(kind: str, data: dict[str, Any]) -> None:
        events.append({"event": kind, "data": json.loads(json.dumps(data, default=str))})

    agent.send(conv, TURNS[0], emit=emit, focus={"claim_id": CLAIM})
    agent.send(conv, TURNS[1], emit=emit)
    sha = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"], cwd=PKG, capture_output=True, text=True
    ).stdout.strip()
    about = (
        f"Recorded from senseai-research@{sha} by scripts/record_events.py (scenario §11 "
        "turns 2-3, recorded model responses). Re-record when research changes a payload."
    )
    doc = {"_about": about, "events": events, "transcript": agent.view(conv)}
    out.write_text(json.dumps(doc, indent=1, ensure_ascii=False) + "\n")
    print(f"{len(events)} events: {sorted({e['event'] for e in events})}")


if __name__ == "__main__":
    main(Path(sys.argv[1]))
