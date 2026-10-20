# SPDX-License-Identifier: Apache-2.0
"""A stand-in for ``senseai-bench replay-compare`` (tests and the headless scenario).

Accepts the same arguments the real command takes, and writes the same files: the
``events.jsonl`` stream, ``replay_report.json`` and per-arm ``timelines/*.npz``. Numbers are
canned but react to the profiles checkout it is given: the Panther arms read the detection
threshold from the **distilled bundle** (``priors/panther_vision.v0.json``), and the run fails
if that bundle disagrees with ``library/felidae.yaml`` (a stale bundle means the workspace did
not re-distill). ``STUB_BENCH_FEW_OBJECTS=1`` writes only five labelled objects.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import numpy as np
import yaml

DURATION_S = 30.0
TICK_S = 0.01


def bundle_threshold(repo: Path) -> float:
    meta = json.loads(
        (repo / "packages" / "senseai-profiles" / "priors" / "panther_vision.v0.json").read_text()
    )
    used = meta["heads"]["frame"]["gain_lut"]["used"]
    return float(next(u["value"] for u in used if u["path"].endswith("detection_threshold")))


def library_threshold(repo: Path) -> float:
    doc = yaml.safe_load((repo / "library" / "felidae.yaml").read_text())
    vision = next(s for s in doc["senses"] if s["id"] == "vision")
    return float(vision["detection_threshold"]["value"])


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd")
    ap.add_argument("--sequence", action="append", required=True)
    ap.add_argument("--arms", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--events", required=True)
    ap.add_argument("--runlog")
    ap.add_argument("--run-id", default="stub")
    ap.add_argument("--profiles-repo")
    ap.add_argument("--max-seconds", type=float)
    a = ap.parse_args(argv)
    assert a.cmd == "replay-compare"
    repo = Path(a.profiles_repo or os.environ["SENSEAI_PROFILES_REPO"])
    arms = a.arms.split(",")
    out = Path(a.out)
    (out / "timelines").mkdir(parents=True, exist_ok=True)
    ev_f = Path(a.events).open("a")  # noqa: SIM115 - kept open for the whole run, like bench

    def emit(t: str, **kw: object) -> None:
        ev_f.write(json.dumps({"type": t, "t_wall": time.time(), "run_id": a.run_id, **kw}) + "\n")
        ev_f.flush()

    thr_b, thr_l = bundle_threshold(repo), library_threshold(repo)
    if abs(thr_b - thr_l) > 1e-9:
        emit("run_end", headline="", error=f"stale bundle: {thr_b} vs library {thr_l}")
        print(f"stale bundle: bundle {thr_b}, library {thr_l}", file=sys.stderr)
        return 1
    few = os.environ.get("STUB_BENCH_FEW_OBJECTS") == "1"
    seqs = [Path(s).name for s in a.sequence]
    emit("run_start", arms=arms, sequences=seqs)
    n_per = 3 if few else 12
    events = [
        {
            "sequence": seq,
            "track_id": i,
            "cls": "pedestrian",
            "t_gt_ns": int((2.0 + 2.3 * i) * 1e9),
            "bearing_deg": 0.0,
            "lux": 0.05,
        }
        for seq in seqs
        for i in range(n_per)
    ]
    t_ns = (np.arange(int(DURATION_S / TICK_S)) * TICK_S * 1e9).astype(np.int64)
    arms_doc = {}
    for arm in arms:
        if arm.startswith("panther"):
            hits_per = 6 if thr_b <= -0.5 else 8  # optics-removed value: more hits in the stub
        elif arm == "null_profile":
            hits_per = 5
        else:
            hits_per = 7
        hits_per = min(hits_per, n_per)
        tracks = hits = 0
        for seq in seqs:
            emit("arm_start", sequence=seq, arm=arm)
            active = np.zeros(t_ns.size, bool)
            active[(np.arange(t_ns.size) % 100) < 10] = True  # background: 10% duty, every 1 s
            bearing = np.where(active, 40.0, np.nan)
            for i in range(n_per):
                t_gt = 2.0 + 2.3 * i
                if i < hits_per:
                    k = int((t_gt - 0.3) / TICK_S)
                    active[k : k + 20] = True
                    bearing[k : k + 20] = 0.0
                tracks += 1
            hits += hits_per
            emit("progress", sequence=seq, arm=arm, frac=0.5)
            np.savez_compressed(
                out / "timelines" / f"{seq}__{arm}.npz",
                t_ns=t_ns,
                active=active,
                latency_ns=np.full(t_ns.size, 2_000_000, np.int64),
                n_boxes=np.zeros(t_ns.size, np.int32),
                top_bearing_deg=bearing.astype(np.float32),
            )
            emit("arm_end", sequence=seq, arm=arm, error=None, summary={})
        arms_doc[arm] = {
            "kind": "reflex" if arm in ("panther_prior", "panther", "null_profile") else "detector",
            "describe": {},
            "errors": [],
            "tracks": tracks,
            "hits": hits,
            "hit_rate": hits / tracks if tracks else None,
            "delay": {"median_s": 0.3 if arm.startswith("panther") else 0.5},
            "unattributed_fires_per_hour": 120.0 if arm.startswith("panther") else 90.0,
            "active_fraction": 0.12,
            "latency": {"p50_ms": 1.5, "p99_ms": 2.0},
            "by_lux": {},
        }
    report = {
        "mode": "labels",
        "config": {"window_pre_s": 2.0, "window_post_s": 1.0, "bearing_tol_deg": 15.0},
        "sequences": [{"name": s, "duration_s": DURATION_S} for s in seqs],
        "events": events,
        "arms": arms_doc,
        "leads": {},
        "n_tracks": len(events),
        "notes": [f"stub bench; bundle threshold {thr_b}"],
    }
    (out / "replay_report.json").write_text(json.dumps(report, indent=1))
    head = " ".join(f"{k}: hit rate {v['hit_rate']:.2f}." for k, v in arms_doc.items())
    emit("run_end", headline=head, report_json=str(out / "replay_report.json"), error=None)
    ev_f.close()
    print(head)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
