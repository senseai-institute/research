# SPDX-License-Identifier: Apache-2.0
"""Compute tools: they run freely within rate limits. They fetch papers, run the extraction
pipeline, store impact records and start bench jobs. They never change a claim's value or
status and never touch the library.
"""

from __future__ import annotations

from typing import Any

from senseai_research import literature
from senseai_research.agent.toolkit import (
    _Q,
    ToolContext,
    ToolError,
    ToolOutput,
    ToolSpec,
    _claim_brief,
    _client,
    _ns,
    _nstrs,
    _quantity_arg,
    _s,
    _source_brief,
    _strs,
)
from senseai_research.experiments.jobs import ExperimentQueue
from senseai_research.ledger.models import Claim, Passage, Source, new_id, now
from senseai_research.literature.passages import locate


def fetch_fulltext(ctx: ToolContext, a: dict[str, Any]) -> ToolOutput:
    from senseai_research.ledger.importers import source_id_for

    doi = a["doi"].strip().lower().removeprefix("https://doi.org/")
    c = _client(ctx)
    paper = literature.lookup(c, doi)
    text = literature.fetch(c, paper)
    if not text.text.strip():
        raise ToolError(f"no open-access text was found for {doi}; only paywalled versions exist")
    sid = source_id_for(doi, None)
    cur = ctx.store.get("source", sid, Source)
    sha = ctx.store.store_text(text.text, [(str(t), int(o)) for t, o in text.sections])
    base = cur or Source(id=sid, created_by="pipeline:fetch@curate", doi=doi)
    src = base.model_copy(
        update={
            "title": base.title if cur and cur.title else paper.title,
            "year": base.year or paper.year,
            "venue": base.venue or (paper.venue or ""),
            "oa_status": "open" if paper.is_oa or text.fulltext else base.oa_status,
            "fulltext_available": text.fulltext,
            "fetched_text_sha256": sha,
            "fetched_from": text.source,
            "retrieved_at": now(),
        }
    )
    ctx.store.put_source(src, "pipeline:fetch@curate")
    res = {
        **_source_brief(src),
        "full_text": text.fulltext,
        "fetched_from": text.source,
        "characters": len(text.text),
        "sections": [t for t, _ in text.sections],
    }
    what = "the full text" if text.fulltext else "only the abstract (no open-access full text)"
    return ToolOutput(res, f"Fetched {what} of {doi}")


def extract_from_source(ctx: ToolContext, a: dict[str, Any]) -> ToolOutput:
    from senseai_research.curate.fetch import PaperText

    src = ctx.store.get("source", a["source_id"], Source)
    if src is None or not src.fetched_text_sha256:
        raise ToolError("fetch the source's text first")
    stored = ctx.store.text(src.fetched_text_sha256)
    assert stored is not None
    text, sections = stored
    pt = PaperText(
        doi=src.doi or src.id,
        text=text,
        fulltext=src.fulltext_available,
        source=src.fetched_from or "ledger",
        sections=tuple(sections),
    )
    ex = literature.extract_path(
        pt,
        a["library_path"],
        cache=literature.model_cache(ctx.cfg.cache_dir / "curate"),
        call=ctx.extract_call,
    )
    actor = "pipeline:extract@curate"
    made = []
    clade = a["library_path"].split(".")[0]
    for cl in ex.claims:
        loc = locate(text, cl.quote, sections)
        if loc is None:
            continue
        p = ctx.store.add_passage(
            Passage(
                id=new_id("psg"),
                created_by=actor,
                source_id=src.id,
                char_start=loc.char_start,
                char_end=loc.char_end,
                text=loc.text,
                section=loc.section or cl.section,
                context_before=loc.context_before,
                context_after=loc.context_after,
            ),
            actor,
        )
        from senseai_research.ledger.importers import quantity_of

        value: Any = cl.value if cl.value is not None else [cl.value_low, cl.value_high]
        c = Claim(
            id=new_id("clm"),
            created_by=actor,
            library_path=f"{clade}.{cl.path}",
            quantity=quantity_of(value, cl.unit),
            species=cl.species,
            conditions=cl.conditions or None,
            source_ids=[src.id],
            passage_id=p.id,
            mapping=cl.mapping if cl.mapping in ("exact", "close", "loose") else "loose",
            status="extracted",
            provenance="pipeline",
        )
        ctx.store.create_claim(c, actor)
        made.append({**_claim_brief(c), "quote": p.text, "flags": cl.flags})
    res = {
        "status": ex.status,
        "claims": made,
        "rejected": [{"reason": why, "quote": r.get("quote")} for r, why in ex.rejected],
    }
    return ToolOutput(res, f"Extracted {len(made)} checked value(s) for {a['library_path']}")


def impact_preview(ctx: ToolContext, a: dict[str, Any]) -> ToolOutput:
    from senseai_research.impact import ImpactError, preview

    try:
        imp = preview(ctx.store, ctx.cfg, a["library_path"], _quantity_arg(a), actor=ctx.actor)
    except ImpactError as e:
        raise ToolError(str(e)) from e
    res = {
        "impact_id": imp.id,
        "library_path": imp.library_path,
        "old": imp.old_quantity,
        "new": imp.new_quantity,
        "read_by": imp.consumers,
        "what_changes": imp.sentences,
        "changed_values": [
            {
                "field": f"{r.profile}.{r.section}.{r.field}",
                "point": r.point,
                "old": r.old,
                "new": r.new,
                "change_pct": None if r.delta_pct is None else round(r.delta_pct, 1),
            }
            for r in imp.profile_rows
        ],
        "reflex": [r.model_dump() for r in imp.reflex_rows if r.theta_old != r.theta_new],
        "world_prior": imp.world_note,
        "world_prior_reached": imp.world_reached,
    }
    return ToolOutput(res, f"Worked out what changing {imp.library_path} does (record {imp.id})")


def experiment_run(ctx: ToolContext, a: dict[str, Any]) -> ToolOutput:
    from senseai_research.experiments import Change, ExperimentSpec

    if ctx.queue is None:
        ctx.queue = ExperimentQueue(ctx.store, ctx.cfg)
    changes = []
    if a["library_path"]:
        changes.append(Change(a["library_path"], _quantity_arg(a)))
    spec = ExperimentSpec(
        sequences=list(a["sequences"]),
        arms=list(a["arms"] or ["panther_prior", "null_profile"]),
        changes=changes,
        claim_ids=list(a["claim_ids"] or []),
        hypothesis_id=a["hypothesis_id"],
    )
    if not spec.changes and not spec.claim_ids:
        raise ToolError("say what to change: claim_ids, or a library_path with a new value")
    job = ctx.queue.submit(spec, ctx.actor)

    def forward(msg: dict[str, Any]) -> None:
        if msg.get("job_id") == job.id:
            ctx.emit("job_progress", msg)

    unsubscribe = ctx.queue.subscribe(forward)
    try:
        ctx.queue.wait(job.id, ctx.experiment_wait_s)
    finally:
        unsubscribe()
    res = {
        "experiment_id": job.id,
        "status": job.status,
        "baseline_experiment_id": job.baseline_id,
        "error": job.error,
        "next": "Read the results with experiment_get." if job.status == "done" else "",
    }
    if job.status not in ("done", "failed"):
        res["next"] = "Still running. Check later with experiment_get."
    return ToolOutput(res, f"Experiment {job.id}: {job.status}")


COMPUTE_TOOLS: tuple[ToolSpec, ...] = (
    ToolSpec(
        "fetch_fulltext",
        "compute",
        "Fetch a paper's open-access full text (never paywalled content) and store it in the "
        "ledger as a source. Says when only the abstract is available.",
        {"doi": _s("the DOI, e.g. 10.1152/jn.90641.2008")},
        fetch_fulltext,
        lambda a: f"Fetching the full text of {a.get('doi')}",
        rate=(10, 600.0),
    ),
    ToolSpec(
        "extract_from_source",
        "compute",
        "Run the curate extraction on one fetched source for one library path. Values whose "
        "quote is verified in the text become 'extracted' claims (provenance: pipeline).",
        {
            "source_id": _s("a source with fetched text"),
            "library_path": _s("full library path, e.g. felidae.senses.vision.detection_threshold"),
        },
        extract_from_source,
        lambda a: f"Extracting {a.get('library_path')} from {a.get('source_id')}",
        rate=(5, 600.0),
    ),
    ToolSpec(
        "impact_preview",
        "compute",
        "Work out, in code, what a new library value does: which profile fields read it, every "
        "re-derived profile value that changes, the reflex threshold per arm, and whether the "
        "world prior reads it. Stores the result; quote it, do not estimate.",
        {"library_path": _s("full library path"), **_Q},
        impact_preview,
        lambda a: f"Working out what changing {a.get('library_path')} does to Panther",
        rate=(20, 600.0),
    ),
    ToolSpec(
        "experiment_run",
        "compute",
        "Run bench replay-compare on night recordings with the changed library, against the "
        "cached unchanged baseline. Takes minutes; progress is shown to the person. Give either "
        "claim_ids (use their current values) or a library_path with a new value.",
        {
            "claim_ids": _nstrs("claims whose current values to test"),
            "library_path": _ns("a path to change instead of claim_ids"),
            **_Q,
            "sequences": _strs("recording names, e.g. zurich_city_03_a"),
            "arms": _nstrs("bench arms (default panther_prior and null_profile)"),
            "hypothesis_id": _ns("the hypothesis this tests, if any"),
        },
        experiment_run,
        lambda a: f"Running the bench on {', '.join(a.get('sequences') or [])}",
        rate=(3, 3600.0),
    ),
)

__all__ = ["COMPUTE_TOOLS"]
