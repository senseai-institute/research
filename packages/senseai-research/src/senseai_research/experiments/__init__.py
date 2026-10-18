# SPDX-License-Identifier: Apache-2.0
"""Experiments: bench replay-compare on a patched library vs a cached baseline (spec §6).

:func:`run_experiment` is synchronous (the CLI uses it directly). The server and the agent go
through :class:`~senseai_research.experiments.jobs.ExperimentQueue`, which runs one experiment
at a time in the background and streams progress.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from senseai_research import libyaml
from senseai_research.config import Settings
from senseai_research.experiments import bench
from senseai_research.experiments.record import cache_key, outputs_from_report
from senseai_research.experiments.workspace import make_workspace
from senseai_research.impact import git_sha, patch_text
from senseai_research.impact.consumers import ProfilesTree
from senseai_research.impact.derive import ARM_PROFILES
from senseai_research.ledger.models import Claim, Experiment, new_id, now
from senseai_research.ledger.store import Store

DEFAULT_ARMS = ("panther_prior", "null_profile")
Progress = Callable[[dict[str, Any]], None]


class ExperimentError(ValueError):
    pass


@dataclass
class Change:
    library_path: str
    quantity: Mapping[str, Any]  # value, or low+high; unit


@dataclass
class ExperimentSpec:
    sequences: list[str]
    arms: list[str] = field(default_factory=lambda: list(DEFAULT_ARMS))
    changes: list[Change] = field(default_factory=list)
    claim_ids: list[str] = field(default_factory=list)
    hypothesis_id: str | None = None
    max_seconds: float | None = None


def changes_from_claims(store: Store, claim_ids: Sequence[str]) -> list[Change]:
    out = []
    for cid in claim_ids:
        c = store.must("claim", cid, Claim)
        q = c.quantity
        out.append(
            Change(
                c.library_path,
                {"value": q.value, "low": q.low, "high": q.high, "unit": q.unit},
            )
        )
    return out


def patch_library(tree: ProfilesTree, changes: Sequence[Change]) -> tuple[str, str, str]:
    """``(repo-relative file, new text, unified diff)``; all changes must hit one file."""
    files = {tree.library_file(c.library_path) for c in changes}
    if len(files) != 1:
        raise ExperimentError("one experiment changes one library file")
    (f,) = files
    rel = f.relative_to(tree.root).as_posix()
    original = f.read_text()
    text = original
    for c in changes:
        text = patch_text(text, c.library_path, c.quantity)
    return rel, text, libyaml.unified_diff(original, text, rel)


def stack_shas(cfg: Settings) -> dict[str, str]:
    return {"world": git_sha(cfg.stack / "world"), "bench": git_sha(cfg.stack / "bench")}


def _safe(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", s)[:80]


def _run_one(
    store: Store,
    cfg: Settings,
    exp: Experiment,
    *,
    seq_dirs: list[Path],
    library_file: str | None,
    new_text: str | None,
    max_seconds: float | None,
    actor: str,
    progress: Progress,
) -> Experiment:
    if cfg.profiles_repo is None:
        raise ExperimentError("SENSEAI_PROFILES_REPO is not set (a profiles checkout is needed)")
    label = "baseline" if exp.is_baseline else "patched"
    run_dir = cfg.runs_dir / exp.id
    run_dir.mkdir(parents=True, exist_ok=True)
    exp = exp.model_copy(update={"status": "running", "started": now(), "run_dir": str(run_dir)})
    store.put_experiment(exp, actor)
    progress({"experiment_id": exp.id, "phase": label, "message": f"preparing the {label} run"})
    profiles = sorted({ARM_PROFILES[a] for a in exp.arms if a in ARM_PROFILES})
    ws = make_workspace(
        cfg.profiles_repo,
        profiles=profiles,
        library_file=library_file,
        new_text=new_text,
        python=cfg.bench_python,
        parent=_tmp_dir(cfg),
    )
    try:
        events = run_dir / "events.jsonl"
        report_dir = run_dir / "report"
        cmd = bench.compare_command(
            cfg,
            sequences=seq_dirs,
            arms=exp.arms,
            out_dir=report_dir,
            events=events,
            runlog_dir=run_dir / "runlog",
            run_id=exp.id,
            max_seconds=max_seconds,
            profiles_repo=ws.root,
        )
        exp = exp.model_copy(update={"command": cmd, "profiles_sha": ws.sha})
        store.put_experiment(exp, actor)
        total = max(1, len(seq_dirs) * len(exp.arms))
        done = [0]
        frac = [0.0]
        now_ = [""]

        def on_event(ev: dict[str, Any]) -> None:
            t = ev.get("type")
            if t == "arm_start":
                frac[0], now_[0] = 0.0, f"{ev.get('arm')} on {ev.get('sequence')}"
            elif t == "progress":
                frac[0] = float(ev.get("frac") or 0.0)
            elif t == "arm_end":
                done[0] += 1
                frac[0] = 0.0
            else:
                return
            progress(
                {
                    "experiment_id": exp.id,
                    "phase": label,
                    "frac": (done[0] + frac[0]) / total,
                    "message": f"{label}: running {now_[0]}" if now_[0] else label,
                }
            )

        r = bench.run(
            cmd,
            run_dir=run_dir,
            events=events,
            report_dir=report_dir,
            env={"SENSEAI_PROFILES_REPO": str(ws.root)},
            on_event=on_event,
        )
        if r.report is None:
            raise bench.BenchError("senseai-bench wrote no replay_report.json")
        outputs = outputs_from_report(r.report, report_dir)
        end: dict[str, Any] = next(
            (e for e in reversed(bench.read_events(events)) if e.get("type") == "run_end"), {}
        )
        exp = exp.model_copy(
            update={
                "status": "done",
                "finished": now(),
                "outputs": outputs,
                "report_headline": str(end.get("headline") or ""),
            }
        )
    except Exception as e:
        exp = exp.model_copy(update={"status": "failed", "finished": now(), "error": str(e)})
        store.put_experiment(exp, actor)
        raise
    finally:
        ws.cleanup()
    store.put_experiment(exp, actor)
    progress({"experiment_id": exp.id, "phase": label, "frac": 1.0, "message": f"{label} done"})
    return exp


def _tmp_dir(cfg: Settings) -> Path:
    d = cfg.data / "tmp"
    d.mkdir(parents=True, exist_ok=True)
    return d


def find_baseline(store: Store, key: str) -> Experiment | None:
    for e in sorted(store.all("experiment", Experiment), key=lambda e: e.id, reverse=True):
        if e.is_baseline and e.cache_key == key and e.status == "done":
            return e
    return None


def run_experiment(
    store: Store,
    cfg: Settings,
    spec: ExperimentSpec,
    *,
    actor: str,
    progress: Progress | None = None,
    exp_id: str | None = None,
) -> tuple[Experiment, Experiment]:
    """Run (or reuse) the baseline, then the patched run. Returns ``(baseline, patched)``."""
    say = progress or (lambda _p: None)
    if cfg.profiles_repo is None:
        raise ExperimentError("SENSEAI_PROFILES_REPO is not set (a profiles checkout is needed)")
    unknown = [a for a in spec.arms if a not in (*ARM_PROFILES, "torchvision", "yolo")]
    if unknown:
        raise ExperimentError(f"unknown arm(s): {', '.join(unknown)}")
    changes = list(spec.changes) + changes_from_claims(store, spec.claim_ids)
    if not changes:
        raise ExperimentError("an experiment needs a library change (a value or claim ids)")
    tree = ProfilesTree(cfg.profiles_repo)
    rel, new_text, diff = patch_library(tree, changes)
    seq_dirs = bench.find_sequences(cfg.data_root, spec.sequences)
    shas = {"profiles": git_sha(cfg.profiles_repo), **stack_shas(cfg)}
    key = cache_key([p.name for p in seq_dirs], spec.arms, shas, {"max_seconds": spec.max_seconds})
    common: dict[str, Any] = {
        "sequences": [p.name for p in seq_dirs],
        "arms": list(spec.arms),
        "world_sha": shas["world"],
        "bench_sha": shas["bench"],
        "profiles_sha": shas["profiles"],
        "seed": 0,  # replay is deterministic; bench takes no seed
    }
    baseline = find_baseline(store, key)
    if baseline is None:
        say({"phase": "baseline", "message": "no cached baseline for these recordings; running it"})
        b = Experiment(
            id=new_id("exp"), created_by=actor, is_baseline=True, cache_key=key, **common
        )
        store.put_experiment(b, actor)
        baseline = _run_one(
            store,
            cfg,
            b,
            seq_dirs=seq_dirs,
            library_file=None,
            new_text=None,
            max_seconds=spec.max_seconds,
            actor=actor,
            progress=say,
        )
    else:
        say({"phase": "baseline", "message": f"reusing cached baseline {baseline.id}"})
    patched = Experiment(
        id=exp_id or new_id("exp"),
        created_by=actor,
        hypothesis_id=spec.hypothesis_id,
        claim_ids=list(spec.claim_ids),
        library_patch=diff,
        library_patch_sha=libyaml.sha(new_text),
        baseline_experiment_id=baseline.id,
        cache_key=key,
        **common,
    )
    store.put_experiment(patched, actor)
    patched = _run_one(
        store,
        cfg,
        patched,
        seq_dirs=seq_dirs,
        library_file=rel,
        new_text=new_text,
        max_seconds=spec.max_seconds,
        actor=actor,
        progress=say,
    )
    return baseline, patched


__all__ = [
    "DEFAULT_ARMS",
    "Change",
    "ExperimentError",
    "ExperimentSpec",
    "changes_from_claims",
    "find_baseline",
    "patch_library",
    "run_experiment",
    "stack_shas",
]
