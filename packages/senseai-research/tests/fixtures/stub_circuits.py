# SPDX-License-Identifier: Apache-2.0
"""Stand-in for ``senseai-circuits assess -`` and ``catalogue --json`` (circuits af4ac69).

One rule per level, enough to exercise research's side: hearing spatial_resolution in deg
sets ``ts.sigma_bearing_deg`` (knob); ``latency_ms`` fits a circuit; touch is new; the rest
is none. Priority is likelihood × quality × the level's score, as circuits computes it.
"""

import json
import sys

SCORE = {"knob": 1.0, "circuit": 0.3, "new": 0.15, "none": 0.0}


def assess(c: dict) -> dict:
    ev = c["evidence"][0]
    field = ev["library_path"].rsplit(".", 1)[-1]
    level, knob, value = "none", None, None
    if field == "spatial_resolution" and c.get("modality") == "hearing" and ev.get("unit") == "deg":
        level, knob, value = "knob", "ts.sigma_bearing_deg", float(ev["value"])
    elif field == "latency_ms":
        level = "circuit"
    elif c.get("modality") == "touch":
        level = "new"
    s = c["scores"]
    return {
        "candidate_id": c["id"],
        "level": level,
        "circuit": "ts" if level in ("knob", "circuit") else None,
        "knob": knob,
        "value": value,
        "score": SCORE[level],
        "priority": round(s["likelihood"] * s["quality"] * SCORE[level], 4),
        "checks": [],
        "summary": f"{level} (stub)",
    }


if __name__ == "__main__":
    args = sys.argv[1:]
    if args == ["assess", "-"]:
        data = json.load(sys.stdin)
        print(json.dumps([assess(c) for c in data]))
    elif args[:1] == ["catalogue"]:
        print(json.dumps({"circuits_version": "0.0-stub", "circuits": [], "measures": []}))
    else:
        sys.exit(2)
