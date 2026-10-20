# Upstream requests

This repo does not change bench, world, sdk, reflex, tokens or ingest. When it needs something
from them, the request is written here with a proposed patch for that repo's owners.

## 1. bench: report a chance hit rate per arm

**Why.** Spec §1 asks for the luck check beside every hit rate. bench `d7c963a` reports
`hit_rate` but no chance rate. `senseai_research/experiments/chance.py` computes one from bench's
own timeline files, but it belongs where the scoring is.

**What.** In `senseai_bench/replay/score.py::score_arms`, for each arm, shift every sequence's
`active` (and bearings) circularly by `k/(K+1)` of its length for `k = 1..K` (K = 19), re-run
`reaction()` against the same `TrackEvent`s, and report the mean hit rate as `chance_hit_rate`.
For detector arms, shift the per-frame `corridor_boxes` lists together with `active`.

```diff
--- a/packages/senseai-bench/src/senseai_bench/replay/score.py
+++ b/packages/senseai-bench/src/senseai_bench/replay/score.py
@@ def score_arms(
         per[n] = {
             ...
             "hit_rate": hits / len(d) if d else None,
+            "chance_hit_rate": chance_hit_rate(arms[n], events, tracks, cfg),
             ...
         }
+
+
+def chance_hit_rate(results, events, tracks, cfg, shifts: int = 19) -> float | None:
+    """Mean hit rate with each arm's activity shifted circularly in time (the luck check)."""
+    total = sum(len(events.get(r.sequence, [])) for r in results if not r.error)
+    if not total:
+        return None
+    hits = 0
+    for k in range(1, shifts + 1):
+        for r in results:
+            if r.error:
+                continue
+            s = round(r.active.size * k / (shifts + 1))
+            shifted = dataclasses.replace(
+                r,
+                active=np.roll(r.active, s),
+                corridor_boxes=r.corridor_boxes[-s:] + r.corridor_boxes[:-s] if r.corridor_boxes else [],
+                bearings_deg=r.bearings_deg[-s:] + r.bearings_deg[:-s] if r.bearings_deg else [],
+            )
+            for ev in events.get(r.sequence, []):
+                t, _ = reaction(shifted, ev, tracks[r.sequence], cfg)
+                hits += t is not None
+    return hits / (total * shifts)
```

The report table and `headline()` would print it next to `hit_rate`. Once bench writes it,
research uses bench's value and stops computing its own (`experiments/record.py` already
prefers a `chance_hit_rate` field when one is there).

## 2. world: read the vision values the profiles already read

**Why.** On world `8ec2cfd`, `PriorParams.from_library` reads only
`senses[hearing].spatial_resolution`. A correction to `felidae.senses.vision.detection_threshold`
changes Panther's profile but not the world prior. Every impact preview says so, and the gap is
logged as hypothesis `hyp:world-gap:<path>`.

**What.** No patch is proposed: which prior parameter a vision value should set (for example the
matcher's tolerance at low light) is a modelling decision for world's owners. The impact code
needs no change when they make it. `from_library` is re-run on both libraries, so any parameter
it starts to read shows up in the world-prior rows automatically.
