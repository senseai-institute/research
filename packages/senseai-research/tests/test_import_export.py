# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
import yaml

from senseai_research.ledger.export import export, load_export
from senseai_research.ledger.importers import (
    import_library,
    parse_citations,
)
from senseai_research.ledger.models import Claim, Hypothesis, Passage, Source
from senseai_research.ledger.store import Store
from tests.conftest import real_profiles


def _quantity_paths(root: Path) -> list[str]:
    from senseai_profiles.library import load_library

    lib = load_library(root / "library")
    return sorted(q.path for c in lib.values() for q in c.quantities())


def test_one_claim_per_library_quantity(imported: Store, profiles_root: Path) -> None:
    paths = _quantity_paths(profiles_root)
    claims = imported.claims()
    assert sorted(c.library_path for c in claims) == paths
    assert len(paths) > 10


@pytest.mark.skipif(real_profiles() is None, reason="needs a full profiles checkout")
def test_importing_profiles_main(store: Store) -> None:
    root = real_profiles()
    assert root is not None
    res = import_library(store, root)
    assert sorted(c.library_path for c in store.claims()) == _quantity_paths(root)
    from senseai_profiles.research import ResearchPaths, load_requests

    open_reqs = [r for r in load_requests(ResearchPaths(root)).values() if r["status"] == "queued"]
    hyps = store.all("hypothesis", Hypothesis)
    assert {f"request:{r['id']}" for r in open_reqs} <= {h.origin for h in hyps}
    assert res.claims_created


def test_detection_threshold_is_unverified_abstract_only(imported: Store) -> None:
    c = imported.must("claim", "lib:felidae.senses.vision.detection_threshold", Claim)
    assert c.status == "unverified"
    assert c.quantity.value == -0.74 and c.quantity.unit == "log10 re human"
    assert c.unverified_reason is not None and "abstract" in c.unverified_reason
    src = imported.must("source", "src:10.1152/jn.90641.2008", Source)
    assert src.year == 2009 and "Contrast sensitivity" in src.title
    assert src.url == "https://pmc.ncbi.nlm.nih.gov/articles/PMC2724355"
    assert c.source_ids == ["src:10.1152/jn.90641.2008"]  # DOI and PMC link are one paper
    assert c.method == "behavioural"
    assert c.library_notes and "optics" in c.library_notes


def test_ranges_and_canonical_units(imported: Store) -> None:
    c = imported.must("claim", "lib:felidae.senses.hearing.range", Claim)
    assert (c.quantity.low, c.quantity.high, c.quantity.unit) == (48, 85000, "Hz")
    assert c.quantity.canonical_unit == "Hz"


def test_hypotheses_from_requests_and_open_questions(imported: Store, profiles_root: Path) -> None:
    hyps = {h.id: h for h in imported.all("hypothesis", Hypothesis)}
    vq = hyps["hyp:request:felidae-vision-q-1"]
    assert vq.priority == "critical" and "Flicker-fusion" in vq.statement
    assert "panther.frame.gain_lut (vision_supply_vs_demand)" in vq.consumers
    texts = [h.statement for h in hyps.values()]
    assert len(texts) == len(set(texts)), "an open question and its request are one hypothesis"
    assert all(h.status == "open" for h in hyps.values())
    lit = hyps["hyp:request:panther-frame-budget-multiplier-literal"]
    assert lit.priority == "high"


def test_citations_table_parses() -> None:
    from tests.conftest import PROFILES

    rows = parse_citations(PROFILES / "library" / "CITATIONS.md")
    kang = next(r for r in rows if r.doi == "10.1152/jn.90641.2008")
    assert kang.checked == "abstract" and kang.authors.startswith("Kang I")


def test_reimport_is_idempotent_and_upstream_change_is_a_revision(
    tmp_path: Path, store: Store, profiles_root: Path
) -> None:
    root = tmp_path / "profiles"
    shutil.copytree(profiles_root, root)
    import_library(store, root)
    n_events = len(store.events())
    again = import_library(store, root)
    assert not again.claims_created and not again.claims_revised
    assert len(store.events()) == n_events
    f = root / "library" / "felidae.yaml"
    f.write_text(f.read_text().replace("value: 460000", "value: 450000"))
    res = import_library(store, root)
    assert res.claims_revised == ["lib:felidae.senses.vision.measurements.rod_density_peak"]
    (rev,) = store.claim_history(res.claims_revised[0])
    assert rev.changes[0].old["value"] == 460000 and rev.by.startswith("pipeline:import")


def test_curate_run_with_a_checked_quote_makes_an_extracted_claim(
    tmp_path: Path, store: Store, profiles_root: Path
) -> None:
    runs = tmp_path / "runs"
    runs.mkdir()
    text = (
        "Results. Rod density peaked at 460,000 cells/mm2 in a ring around the area "
        "centralis, falling to 275,000 at its centre."
    )
    state = {
        "clade_key": "felidae",
        "senses": ["vision"],
        "curated_at": "2026-10-01",
        "texts": {
            "10.1002/cne.901480209": {
                "doi": "10.1002/cne.901480209",
                "text": text,
                "fulltext": True,
                "source": "europepmc",
                "sections": [["Results", 0]],
            }
        },
        "extractions": {
            "10.1002/cne.901480209": {
                "claims": [
                    {
                        "path": "senses.vision.measurements.rod_density_peak",
                        "value": 460000,
                        "value_low": None,
                        "value_high": None,
                        "quote": "Rod density peaked at 460,000 cells/mm2",
                        "section": "Results",
                        "mapping": "exact",
                    }
                ]
            }
        },
    }
    (runs / "felidae.json").write_text(json.dumps(state))
    import_library(store, profiles_root, runs_dir=runs)
    c = store.must("claim", "lib:felidae.senses.vision.measurements.rod_density_peak", Claim)
    assert c.status == "extracted" and c.provenance == "pipeline" and c.mapping == "exact"
    assert c.passage_id is not None
    p = store.must("passage", c.passage_id, Passage)
    assert p.text == "Rod density peaked at 460,000 cells/mm2" and p.section == "Results"
    src = store.must("source", p.source_id, Source)
    assert src.fulltext_available and src.fetched_text_sha256
    other = store.must("claim", "lib:felidae.senses.vision.measurements.rod_density_central", Claim)
    assert other.status == "unverified"


def test_export_is_stable_and_round_trips(tmp_path: Path, imported: Store) -> None:
    imported.revise_claim(
        "lib:felidae.senses.vision.detection_threshold",
        {"notes_by_reviewer": "check the optics-removed value"},
        reason="reviewer note",
        by="user:senseai",
    )
    a, b = tmp_path / "a", tmp_path / "b"
    export(imported, a)
    export(imported, b)
    for f in sorted(a.rglob("*.yaml")):
        assert f.read_bytes() == (b / f.relative_to(a)).read_bytes()
    doc = load_export(a)
    felidae = {c["id"]: c for c in doc["claims"]["felidae"]}
    dt = felidae["lib:felidae.senses.vision.detection_threshold"]
    assert dt["notes_by_reviewer"] == "check the optics-removed value"
    assert dt["history"][0]["changes"][0]["old"] == ""
    assert len(doc["hypotheses"]) == len(imported.list_raw("hypothesis"))
    yaml.safe_load((a / "sources.yaml").read_text())
