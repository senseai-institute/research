# SPDX-License-Identifier: Apache-2.0
"""Library-change proposals: from an approved action to a PR on profiles (or a download).

:func:`submit` is called only from :func:`senseai_research.actions.approve`, after the
Approval row is stored. It builds the patch against the current profiles checkout, writes the
PR body (spec §7.4: claims with DOI and verbatim quote, the reviewer's rationale, the impact
table, the experiment table, "Approved by …"), and opens the PR when ``GITHUB_TOKEN`` is set.
Without a token it saves ``<id>.patch`` and ``<id>.md`` under ``RESEARCH_DATA/outbox`` for
download.
"""

from __future__ import annotations

from collections.abc import Sequence

from senseai_research.config import Settings
from senseai_research.experiments.record import compare, table
from senseai_research.ledger.models import (
    Approval,
    Claim,
    Experiment,
    Impact,
    Passage,
    PendingAction,
    Proposal,
    Source,
    new_id,
)
from senseai_research.ledger.store import Store
from senseai_research.proposals.diff import Patch, build_patch


def _impact_for(store: Store, cfg: Settings, claim: Claim, actor: str) -> Impact:
    """The latest stored impact for this exact change, else compute and store one (N4)."""
    target = claim.quantity.model_dump()
    for imp in sorted(store.all("impact", Impact), key=lambda i: i.id, reverse=True):
        if imp.library_path != claim.library_path:
            continue
        nq = imp.new_quantity
        if all(nq.get(k) == target.get(k) for k in ("value", "low", "high") if k in nq):
            return imp
    from senseai_research.impact import preview

    return preview(store, cfg, claim.library_path, target, actor=actor)


def _impact_md(imp: Impact) -> str:
    lines = [f"Consumers: {', '.join(imp.consumers) or 'none'}", ""]
    lines += ["| field | point | old | new | Δ% |", "|---|---|---|---|---|"]
    for r in imp.profile_rows:
        d = "" if r.delta_pct is None else f"{r.delta_pct:+.1f}%"
        o = f"{r.old:.4g}" if isinstance(r.old, float) else str(r.old)
        n = f"{r.new:.4g}" if isinstance(r.new, float) else str(r.new)
        lines.append(f"| {r.profile}.{r.section}.{r.field} | {r.point} | {o} | {n} | {d} |")
    if not imp.profile_rows:
        lines.append("| (no profile value changes) | | | | |")
    lines += ["", f"World prior: {imp.world_note}", ""]
    for x in imp.reflex_rows:
        lines.append(
            f"- reflex θ, arm {x.arm}: {x.theta_old:.2f} → {x.theta_new:.2f} "
            f"(commit threshold {x.commit_threshold_old:.2f} → {x.commit_threshold_new:.2f})"
        )
    lines.append(f"\nImpact record `{imp.id}`, profiles `{imp.profiles_sha[:12]}`.")
    return "\n".join(lines)


def _experiments_md(store: Store, ids: Sequence[str]) -> str:
    if not ids:
        return "No experiment was run for this change."
    out = []
    for eid in ids:
        pat = store.must("experiment", eid, Experiment)
        base = (
            store.get("experiment", pat.baseline_experiment_id, Experiment)
            if pat.baseline_experiment_id
            else None
        )
        out += [
            f"**Experiment `{pat.id}`** (baseline `{base.id if base else 'none'}`), "
            f"recordings: {', '.join(pat.sequences)}",
            "",
            table(compare(base, pat)),
            "",
            f"SHAs: profiles `{pat.profiles_sha[:12]}`, world `{pat.world_sha[:12]}`, "
            f"bench `{pat.bench_sha[:12]}`. Hit rates are per labelled object; the chance rate "
            "is the luck check (activity shifted in time).",
            "",
            "Command:",
            "",
            "```",
            " ".join(pat.command),
            "```",
            "",
        ]
    return "\n".join(out)


def pr_body(
    store: Store,
    cfg: Settings,
    claims: Sequence[Claim],
    action: PendingAction,
    ap: Approval,
    patch: Patch,
) -> str:
    parts = ["## Claims", ""]
    for c in claims:
        p = store.must("passage", c.passage_id or "", Passage)
        src = store.must("source", p.source_id, Source)
        ref = src.doi or src.url or src.id
        where = f", {p.section}" if p.section else ""
        parts += [
            f"- **{c.library_path}** = {c.quantity.text()} ({c.species or 'species not stated'}; "
            f"{c.conditions or 'conditions not stated'})",
            f"  - {ref}{where}: “{p.text}”",
        ]
    parts += [
        "",
        "## Reviewer's rationale",
        "",
        action.input.get("rationale", "").strip(),
        "",
        f"> {ap.rationale}",
        "",
        "## Impact",
        "",
    ]
    for c in claims:
        parts += [_impact_md(_impact_for(store, cfg, c, "pipeline:proposal@0.1.0")), ""]
    parts += ["## Experiments", "", _experiments_md(store, action.input.get("experiment_ids", []))]
    parts += [
        "",
        "## Patch",
        "",
        "```diff",
        patch.diff.rstrip(),
        "```",
        "",
        f"Approved by {ap.by.removeprefix('user:')} at {ap.at} in senseai-research.",
    ]
    return "\n".join(parts) + "\n"


def submit(store: Store, cfg: Settings, action: PendingAction, ap: Approval) -> Proposal:
    if cfg.profiles_repo is None:
        raise ValueError("SENSEAI_PROFILES_REPO is not set")
    claims = [store.must("claim", c, Claim) for c in action.input["claim_ids"]]
    patch = build_patch(store, cfg.profiles_repo, claims)
    prop = Proposal(
        id=new_id("prp"),
        created_by=ap.by,
        action_id=action.id,
        claim_ids=[c.id for c in claims],
        library_file=patch.file,
        yaml_patch=patch.diff,
        experiment_ids=list(action.input.get("experiment_ids", [])),
        rationale=action.input.get("rationale", ""),
        status="approved",
        title=patch.title,
    )
    store.put_proposal(prop, ap.by)
    body = pr_body(store, cfg, claims, action, ap, patch)
    impacts = [i.id for i in store.all("impact", Impact) if i.library_path in patch.old_values]
    if cfg.github_token:
        from senseai_research.proposals.github import open_pr

        url = open_pr(store, cfg, prop, file=patch.file, new_text=patch.new_text, body=body)
        prop = prop.model_copy(update={"status": "pr_open", "pr_url": url, "impact_ids": impacts})
    else:
        cfg.outbox.mkdir(parents=True, exist_ok=True)
        pf = cfg.outbox / f"{prop.id}.patch"
        bf = cfg.outbox / f"{prop.id}.md"
        pf.write_text(patch.diff)
        bf.write_text(f"# {patch.title}\n\n{body}")
        prop = prop.model_copy(
            update={"patch_file": str(pf), "pr_body_file": str(bf), "impact_ids": impacts}
        )
    store.put_proposal(prop, ap.by)
    return prop


__all__ = ["pr_body", "submit"]
