# SPDX-License-Identifier: Apache-2.0
"""Read tools: they run freely and change nothing."""

from __future__ import annotations

from typing import Any

from senseai_research import literature
from senseai_research.agent.toolkit import (
    ToolContext,
    ToolError,
    ToolOutput,
    ToolSpec,
    _claim_brief,
    _client,
    _enum_n,
    _ni,
    _ns,
    _s,
    _source_brief,
    _words,
)
from senseai_research.ledger.models import (
    CLAIM_STATUSES,
    Claim,
    Experiment,
    Hypothesis,
    Passage,
    Source,
)
from senseai_research.literature.passages import locate, window


def ledger_search(ctx: ToolContext, a: dict[str, Any]) -> ToolOutput:
    q = (a["query"] or "").lower().strip()
    words = [w for w in q.split() if w]

    def hit(*texts: Any) -> bool:
        blob = " ".join(str(t or "") for t in texts).lower()
        return all(w in blob for w in words)

    claims = [
        c
        for c in ctx.store.claims()
        if (not a["library_path"] or c.library_path.startswith(a["library_path"]))
        and (not a["status"] or c.status == a["status"])
        and hit(c.library_path, c.species, c.conditions, c.library_notes, c.notes_by_reviewer)
    ]
    sources = [
        s
        for s in ctx.store.all("source", Source)
        if words and hit(s.title, s.authors, s.doi, s.url, s.venue)
    ]
    hyps = [
        h
        for h in ctx.store.all("hypothesis", Hypothesis)
        if hit(h.statement, h.target, h.origin)
        and (
            not a["library_path"]
            or (h.target or "").startswith(a["library_path"].rsplit(".", 1)[0])
        )
    ]
    res = {
        "claims": [_claim_brief(c) for c in claims[:25]],
        "sources": [_source_brief(s) for s in sources[:15]],
        "hypotheses": [
            {
                "hypothesis_id": h.id,
                "statement": h.statement,
                "status": h.status,
                "priority": h.priority,
            }
            for h in hyps[:15]
        ],
    }
    return ToolOutput(
        res, f"Found {len(claims)} claims, {len(sources)} sources and {len(hyps)} hypotheses"
    )


def claim_get(ctx: ToolContext, a: dict[str, Any]) -> ToolOutput:
    c = ctx.store.get("claim", a["claim_id"], Claim)
    if c is None:
        raise ToolError(f"there is no claim {a['claim_id']!r}; use ledger_search to find it")
    passage = None
    if c.passage_id:
        p = ctx.store.must("passage", c.passage_id, Passage)
        passage = {
            "source_id": p.source_id,
            "section": p.section,
            "quote": p.text,
            "context_before": p.context_before[-300:],
            "context_after": p.context_after[:300],
        }
    created = ctx.store.claim_created_event(c.id)
    history = [
        {
            "revision": 0,
            "by": created.actor if created else c.created_by,
            "at": created.at if created else c.created_at,
            "value": created.payload["claim"]["quantity"] if created else None,
            "reason": "imported" if created else "",
        }
    ]
    for r in ctx.store.claim_history(c.id):
        history.append(
            {
                "revision": r.revision,
                "by": r.by,
                "at": r.at,
                "reason": r.reason,
                "changes": [{"field": ch.field, "old": ch.old, "new": ch.new} for ch in r.changes],
            }
        )
    res = {
        **_claim_brief(c),
        "method": c.method,
        "mapping": c.mapping,
        "provenance": c.provenance,
        "library_confidence": c.library_confidence,
        "library_notes": c.library_notes,
        "notes_by_reviewer": c.notes_by_reviewer,
        "unverified_reason": c.unverified_reason,
        "sources": [
            _source_brief(s)
            for sid in c.source_ids
            if (s := ctx.store.get("source", sid, Source)) is not None
        ],
        "passage": passage,
        "history": history,
    }
    return ToolOutput(res, f"Read {c.library_path} ({c.status}, revision {c.revision})")


def source_passage(ctx: ToolContext, a: dict[str, Any]) -> ToolOutput:
    src = ctx.store.get("source", a["source_id"], Source)
    if src is None:
        raise ToolError(f"there is no source {a['source_id']!r}")
    stored = ctx.store.text(src.fetched_text_sha256 or "")
    if stored is None:
        raise ToolError(f"{src.id} has no fetched text yet; fetch the full text first")
    text, sections = stored
    if a["quote"]:
        loc = locate(text, a["quote"], sections, around_char=a["around_char"])
        if loc is None:
            raise ToolError(
                "that text is not in the document, word for word. Quote exactly, or search "
                "with a shorter phrase."
            )
    elif a["around_char"] is not None:
        loc = window(text, int(a["around_char"]), sections)
    else:
        raise ToolError("give a quote to find, or a character offset to read around")
    res = {
        "source_id": src.id,
        "doi": src.doi,
        "full_text": src.fulltext_available,
        "section": loc.section,
        "char_start": loc.char_start,
        "char_end": loc.char_end,
        "text": loc.text,
        "context_before": loc.context_before[-400:],
        "context_after": loc.context_after[:400],
    }
    where = f" in {loc.section}" if loc.section else ""
    return ToolOutput(res, f"Found the passage{where} of {src.doi or src.id}")


def literature_search(ctx: ToolContext, a: dict[str, Any]) -> ToolOutput:
    query = " ".join(x for x in (a["query"], a["species"]) if x)
    papers, errors = literature.search(_client(ctx), query, since=a["since"])
    res = {
        "papers": [
            {
                "doi": p.doi,
                "title": p.title,
                "year": p.year,
                "venue": p.venue,
                "open_access": p.is_oa,
                "pmcid": p.pmcid,
                "kind": p.kind,
                "cited_by": p.cited_by,
            }
            for p in papers
        ],
        "sources_that_failed": errors,
    }
    return ToolOutput(res, f"Found {len(papers)} papers for “{query}”")


def experiment_get(ctx: ToolContext, a: dict[str, Any]) -> ToolOutput:
    from senseai_research.experiments.record import compare, table

    e = ctx.store.get("experiment", a["experiment_id"], Experiment)
    if e is None:
        raise ToolError(f"there is no experiment {a['experiment_id']!r}")
    base = (
        ctx.store.get("experiment", e.baseline_experiment_id, Experiment)
        if e.baseline_experiment_id
        else None
    )
    rows = compare(base, e) if e.status == "done" else []
    res = {
        "experiment_id": e.id,
        "status": e.status,
        "error": e.error,
        "baseline_experiment_id": e.baseline_experiment_id,
        "recordings": e.sequences,
        "arms": e.arms,
        "comparison": [r.model_dump() for r in rows],
        "table": table(rows) if rows else "",
        "shas": {"profiles": e.profiles_sha, "world": e.world_sha, "bench": e.bench_sha},
    }
    return ToolOutput(res, f"Read experiment {e.id} ({e.status})")


READ_TOOLS: tuple[ToolSpec, ...] = (
    ToolSpec(
        "ledger_search",
        "read",
        "Search the evidence ledger: claims (library values with status and history), sources "
        "and hypotheses. Words must all appear. Filter by library path prefix and claim status.",
        {
            "query": _s("words to look for; empty string lists everything under the filters"),
            "library_path": _ns("only claims under this path, e.g. felidae.senses.vision"),
            "status": _enum_n(CLAIM_STATUSES, "only claims with this status"),
        },
        ledger_search,
        lambda a: (
            f"Searching the ledger for “{a.get('query') or a.get('library_path') or 'everything'}”"
        ),
    ),
    ToolSpec(
        "claim_get",
        "read",
        "One claim in full: value, units, species, conditions, status, sources, the verbatim "
        "passage with its context, and every revision (who, when, why, the old value).",
        {"claim_id": _s("claim id, e.g. lib:felidae.senses.vision.detection_threshold")},
        claim_get,
        lambda a: f"Reading claim {a.get('claim_id')}",
    ),
    ToolSpec(
        "source_passage",
        "read",
        "Find a quote in a fetched document and return it with the text around it, or read the "
        "text around a character offset. Errors if the quote is not there word for word.",
        {
            "source_id": _s("source id, e.g. src:10.1152/jn.90641.2008"),
            "quote": _ns("exact text to find (null to read around around_char)"),
            "around_char": _ni(
                "character offset to read around, or to prefer when the quote repeats"
            ),
        },
        source_passage,
        lambda a: f"Looking up the passage in {a.get('source_id')}",
    ),
    ToolSpec(
        "literature_search",
        "read",
        "Search the literature (OpenAlex, Europe PMC, PubMed, Semantic Scholar) through the "
        "cached curate client. Returns DOIs, titles, years and whether they are open access.",
        {
            "query": _s("search words, e.g. cat scotopic contrast sensitivity"),
            "species": _ns("species name to add to the query"),
            "since": _ni("earliest publication year"),
        },
        literature_search,
        lambda a: f"Searching the literature for “{_words(a.get('query'), a.get('species'))}”",
    ),
    ToolSpec(
        "experiment_get",
        "read",
        "A stored experiment's results: per arm, baseline and patched hit rate with chance hit "
        "rate, median delay, false fires per hour and p99 latency, or 'too few objects'. These "
        "are the only performance numbers you may state.",
        {"experiment_id": _s("experiment id")},
        experiment_get,
        lambda a: f"Reading the results of experiment {a.get('experiment_id')}",
    ),
)

__all__ = ["READ_TOOLS"]
