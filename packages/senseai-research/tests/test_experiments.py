# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from senseai_research.config import Settings
from senseai_research.experiments import (
    Change,
    ExperimentSpec,
    run_experiment,
)
from senseai_research.experiments.bench import compare_command, find_sequences
from senseai_research.experiments.chance import chance_hit_rate
from senseai_research.experiments.jobs import ExperimentQueue
from senseai_research.experiments.record import MIN_OBJECTS, compare, table
from senseai_research.ledger.models import Experiment
from senseai_research.ledger.store import Store
from tests.conftest import SEQUENCES

PATH = "felidae.senses.vision.detection_threshold"


def _spec(**kw: object) -> ExperimentSpec:
    return ExperimentSpec(
        sequences=list(SEQUENCES),
        arms=["panther_prior", "null_profile"],
        changes=[Change(PATH, {"value": 0.0, "unit": "log10 re human"})],
        **kw,  # type: ignore[arg-type]
    )


@pytest.mark.profiles
def test_experiment_runs_baseline_and_patched_with_chance(store: Store, settings: Settings) -> None:
    progress: list[dict[str, object]] = []
    base, pat = run_experiment(
        store, settings, _spec(), actor="user:senseai", progress=progress.append
    )
    assert base.is_baseline and base.status == "done" and not base.library_patch
    assert pat.status == "done" and pat.baseline_experiment_id == base.id
    assert "-      value: -0.74" in pat.library_patch and "+      value: 0.0" in pat.library_patch
    for e in (base, pat):
        for arm in ("panther_prior", "null_profile"):
            r = e.outputs[arm]
            assert r.chance_hit_rate is not None and 0 < r.chance_hit_rate < 1
            assert r.tracks == 24
    assert base.outputs["panther_prior"].hit_rate == pytest.approx(12 / 24)
    assert pat.outputs["panther_prior"].hit_rate == pytest.approx(16 / 24)  # bundle re-distilled
    assert pat.outputs["null_profile"].hit_rate == base.outputs["null_profile"].hit_rate
    assert pat.command[1].endswith("stub_bench.py") and "replay-compare" in pat.command
    assert "--profiles-repo" in pat.command and pat.sequences == list(SEQUENCES)
    assert any("running panther_prior" in str(p.get("message")) for p in progress)
    assert Path(pat.run_dir or "").joinpath("events.jsonl").exists()


@pytest.mark.profiles
def test_baseline_is_cached_by_key(store: Store, settings: Settings) -> None:
    b1, _ = run_experiment(store, settings, _spec(), actor="user:senseai")
    spec2 = ExperimentSpec(
        sequences=list(SEQUENCES),
        arms=["panther_prior", "null_profile"],
        changes=[Change(PATH, {"value": -0.3, "unit": "log10 re human"})],
    )
    b2, _ = run_experiment(store, settings, spec2, actor="user:senseai")
    assert b2.id == b1.id
    assert sum(1 for e in store.all("experiment", Experiment) if e.is_baseline) == 1
    b3, _ = run_experiment(
        store,
        settings,
        ExperimentSpec(sequences=[SEQUENCES[0]], arms=["panther_prior"], changes=spec2.changes),
        actor="user:senseai",
    )
    assert b3.id != b1.id


@pytest.mark.profiles
def test_too_few_objects_is_not_a_percentage(
    store: Store, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("STUB_BENCH_FEW_OBJECTS", "1")
    base, pat = run_experiment(store, settings, _spec(), actor="user:senseai")
    rows = compare(base, pat)
    assert all(not r.judged and r.patched_hit_rate is None for r in rows)
    assert "too few objects to judge" in rows[0].verdict
    assert f"need {MIN_OBJECTS}" in rows[0].verdict
    assert "too few objects to judge" in table(rows)


@pytest.mark.profiles
def test_queue_runs_one_at_a_time_and_reports_progress(store: Store, settings: Settings) -> None:
    q = ExperimentQueue(store, settings)
    seen: list[dict[str, object]] = []
    q.subscribe(seen.append)
    j1 = q.submit(_spec(), "user:senseai")
    j2 = q.submit(_spec(), "user:senseai")
    assert q.wait(j1.id, 120).status == "done"
    assert q.wait(j2.id, 120).status == "done"
    exp = store.must("experiment", j2.id, Experiment)
    assert exp.status == "done" and exp.baseline_experiment_id == j1.baseline_id
    assert any(m.get("job_id") == j1.id and "running" in str(m.get("message")) for m in seen)
    q.close()


def test_failed_bench_marks_the_experiment_failed(store: Store, settings: Settings) -> None:
    bad = settings.with_(bench_cmd="false")
    q = ExperimentQueue(store, bad)
    job = q.submit(_spec(), "user:senseai")
    assert q.wait(job.id, 60).status == "failed"
    q.close()


def test_command_matches_the_viewer_builder(settings: Settings, tmp_path: Path) -> None:
    seqs = find_sequences(settings.data_root, list(SEQUENCES))
    cmd = compare_command(
        settings.with_(bench_cmd="senseai-bench"),
        sequences=seqs,
        arms=["panther_prior", "null_profile"],
        out_dir=tmp_path / "report",
        events=tmp_path / "events.jsonl",
        runlog_dir=tmp_path / "runlog",
        run_id="exp-1",
        profiles_repo=tmp_path / "ws",
    )
    assert cmd[:2] == ["senseai-bench", "replay-compare"]
    assert cmd[2:6] == ["--sequence", str(seqs[0]), "--sequence", str(seqs[1])]
    assert cmd[6:8] == ["--arms", "panther_prior,null_profile"]
    assert cmd[-2:] == ["--run-id", "exp-1"] and f"{tmp_path / 'runlog'}/" in cmd


def test_missing_recording_is_explained(settings: Settings) -> None:
    from senseai_research.experiments.bench import BenchError

    with pytest.raises(BenchError, match="not on this machine"):
        find_sequences(settings.data_root, ["zurich_city_42_z"])


def test_chance_hit_rate_by_circular_shift(tmp_path: Path) -> None:
    t_ns = (np.arange(1000) * 1e7).astype(np.int64)  # 10 s at 10 ms
    active = np.zeros(1000, bool)
    active[::100] = True  # one tick per second
    (tmp_path / "tl").mkdir()
    np.savez(tmp_path / "tl" / "s__a.npz", t_ns=t_ns, active=active, top_bearing_deg=np.zeros(1000))
    report = {
        "config": {"window_pre_s": 0.2, "window_post_s": 0.2, "bearing_tol_deg": 15},
        "events": [
            {"sequence": "s", "track_id": i, "t_gt_ns": int((0.55 + i) * 1e9), "bearing_deg": 0}
            for i in range(5)
        ],
        "arms": {"a": {"kind": "detector"}},
    }
    rate = chance_hit_rate(report, tmp_path / "tl", "a")
    assert rate is not None and 0.2 < rate < 0.6  # 0.4 s window, one tick per second
    report["arms"]["a"]["chance_hit_rate"] = 0.123
    assert chance_hit_rate(report, tmp_path / "missing", "a") is None
    _ = json.dumps(report)
