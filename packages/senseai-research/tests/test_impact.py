# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from senseai_research import libyaml
from senseai_research.config import Settings
from senseai_research.impact import WORLD_GAP, ImpactError, preview
from senseai_research.ledger.models import Hypothesis, Impact
from senseai_research.ledger.store import Store
from tests.conftest import FIXTURES, PROFILES

pytestmark = pytest.mark.profiles

PATH = "felidae.senses.vision.detection_threshold"
GOLDEN = FIXTURES / "golden" / "impact_detection_threshold.json"


def _optics_removed(store: Store, settings: Settings) -> Impact:
    return preview(
        store, settings, PATH, {"value": 0.0, "unit": "log10 re human"}, actor="user:senseai"
    )


def test_n4_consumers_come_from_consumers_by_path(store: Store, settings: Settings) -> None:
    from senseai_profiles.library import load_library
    from senseai_profiles.research import consumers_by_path
    from senseai_profiles.spec import load_specs

    imp = _optics_removed(store, settings)
    lib = load_library(PROFILES / "library")
    specs = load_specs(PROFILES / "packages" / "senseai-profiles" / "profiles", lib)
    expected = sorted(c.label for c in consumers_by_path(specs)[PATH])
    assert (
        imp.consumers
        == expected
        == [
            "panther.event.attention (vision_deficit_boost)",
            "panther.frame.gain_lut (vision_supply_vs_demand)",
            "panther_vision.event.attention (vision_deficit_boost)",
            "panther_vision.frame.gain_lut (vision_supply_vs_demand)",
        ]
    )


def test_n4_profile_values_are_re_derived_not_estimated(store: Store, settings: Settings) -> None:
    """The new gain-LUT values equal what the rules give on a patched library, point by point."""
    import shutil
    import tempfile

    from senseai_research.impact import derive as dv
    from senseai_research.impact.consumers import ProfilesTree

    imp = _optics_removed(store, settings)
    tree = ProfilesTree(PROFILES)
    with tempfile.TemporaryDirectory() as tmp:
        lib = Path(tmp) / "library"
        shutil.copytree(tree.library, lib)
        f = lib / "felidae.yaml"
        f.write_text(libyaml.set_quantity(f.read_text(), PATH, {"value": 0.0}))
        after = dv.derive(tree, lib)
    _, table = dv.flatten(after["panther"]["heads"][("frame", "gain_lut")][1])
    for row in imp.profile_rows:
        if row.profile == "panther" and row.field == "gain_lut":
            assert row.new == table[row.point]
    stored = store.must("impact", imp.id, Impact)
    assert stored == imp, "the impact record is stored as computed"


def test_golden_impact_of_the_optics_removed_threshold(store: Store, settings: Settings) -> None:
    imp = _optics_removed(store, settings)
    doc = {
        "consumers": imp.consumers,
        "rows": [r.model_dump() for r in imp.profile_rows],
        "summaries": [s.model_dump() for s in imp.field_summaries],
        "unchanged_fields": imp.unchanged_fields,
        "reflex": [r.model_dump() for r in imp.reflex_rows],
        "world_reached": imp.world_reached,
    }
    if os.environ.get("UPDATE_GOLDEN") == "1":
        GOLDEN.write_text(json.dumps(doc, indent=1, sort_keys=True) + "\n")
    golden = json.loads(GOLDEN.read_text())
    assert json.loads(json.dumps(doc, sort_keys=True)) == golden
    gain = next(s for s in imp.field_summaries if s.profile == "panther" and s.field == "gain_lut")
    assert "down to 0.316 lux (was 0.0575 lux)" in gain.sentence


def test_vision_change_does_not_reach_the_world_prior(store: Store, settings: Settings) -> None:
    imp = _optics_removed(store, settings)
    assert imp.world_available and not imp.world_reached
    assert WORLD_GAP in imp.world_note
    sigma = next(r for r in imp.world_rows if r.param == "sigma_bearing_deg")
    assert sigma.measured and not sigma.changed
    gap = store.must("hypothesis", f"hyp:world-gap:{PATH}", Hypothesis)
    assert gap.origin == f"impact:{imp.id}" and gap.status == "open"


def test_hearing_acuity_does_reach_the_world_prior(store: Store, settings: Settings) -> None:
    imp = preview(
        store,
        settings,
        "felidae.senses.hearing.spatial_resolution",
        {"value": 4.0, "unit": "deg"},
        actor="user:senseai",
    )
    assert imp.world_reached and not imp.consumers
    sigma = next(r for r in imp.world_rows if r.param == "sigma_bearing_deg")
    assert (sigma.old, sigma.new, sigma.changed) == (5.0, 4.0, True)
    assert "No profile value changes" in imp.sentences[0]


def test_reflex_theta_follows_d1(store: Store, settings: Settings) -> None:
    imp = preview(
        store,
        settings,
        "felidae.behaviours.acoustic_orienting.latency_ms",
        {"value": 50.0, "unit": "ms"},
        actor="user:senseai",
    )
    panther = next(r for r in imp.reflex_rows if r.arm == "panther")
    assert panther.commit_threshold_old == pytest.approx(0.6)
    assert panther.commit_threshold_new == pytest.approx(0.8)
    assert panther.theta_old == pytest.approx(3.12) and panther.theta_new == pytest.approx(4.16)
    prior = next(r for r in imp.reflex_rows if r.arm == "panther_prior")
    assert prior.theta_old == prior.theta_new == pytest.approx(2.6)


def test_world_missing_is_said_not_guessed(store: Store, settings: Settings) -> None:
    imp = _optics_removed(store, settings.with_(world_params=None))
    assert not imp.world_available and imp.world_rows == []
    assert "not installed" in imp.world_note


def test_wrong_unit_is_refused(store: Store, settings: Settings) -> None:
    with pytest.raises(ImpactError, match="log10 re human"):
        preview(store, settings, PATH, {"value": 5.5, "unit": "ratio"}, actor="user:x")


def test_library_edit_keeps_the_file_byte_for_byte_elsewhere() -> None:
    old = (PROFILES / "library" / "felidae.yaml").read_text()
    new = libyaml.set_quantity(old, PATH, {"value": 0.0})
    diff = libyaml.unified_diff(old, new, "library/felidae.yaml")
    changed = [ln for ln in diff.splitlines() if ln[:1] in "+-" and ln[:3] not in ("+++", "---")]
    assert changed == ["-      value: -0.74", "+      value: 0.0"]
