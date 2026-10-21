# SPDX-License-Identifier: Apache-2.0
"""Release a handoff package to circuits, only after a person approves it (N1).

Approving a ``handoff`` action rebuilds the package and checks it is the one the person saw
(same digest), then:

1. writes ``RESEARCH_DATA/outbox/handoff/<clade>.<digest[:12]>.json`` (always), which
   ``GET /api/research/handoff/{clade}/released`` serves;
2. when ``RESEARCH_CIRCUITS_REPO`` is set, pushes it as ``research/<clade>.handoff.json`` on
   branch ``research/handoff-<clade>-<digest[:12]>`` of that repository and opens a pull
   request (never onto its default branch);
3. records a ``HandoffRelease`` in the ledger.

The push clones the remote, not a local checkout, with ``RESEARCH_CIRCUITS_TOKEN`` (or
``GITHUB_TOKEN``), the same way ``proposals.github`` opens profiles PRs.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import httpx

from senseai_research.config import Settings
from senseai_research.handoff.package import Handoff
from senseai_research.ledger.models import Approval, HandoffRelease, PendingAction, new_id
from senseai_research.ledger.store import LedgerError, Store
from senseai_research.proposals import github
from senseai_research.proposals.github import GitHubError, _git

PR_FILE = "research/{clade}.handoff.json"


def outbox(cfg: Settings) -> Path:
    return cfg.outbox / "handoff"


def _remote(cfg: Settings, token: str) -> str:
    if cfg.circuits_remote:
        return cfg.circuits_remote
    return f"https://x-access-token:{token}@github.com/{cfg.circuits_repo}.git"


def pr_body(h: Handoff, ap: Approval) -> str:
    who = ap.by.removeprefix("user:")
    lines = [
        f"Evidence handoff for **{h.clade}** from senseai-research.",
        "",
        *h.summary,
        "",
        "| knob | value | readiness | strength | default |",
        "|---|---|---|---|---|",
    ]
    for k in h.knobs:
        if k.estimate is None:
            continue
        v = h.circuits.get(k.knob, "—")
        lines.append(
            f"| `{k.knob}` | {v} | {k.estimate.readiness} | {k.estimate.strength:.2f} "
            f"| {k.default} |"
        )
    lines += ["", "Gaps:", *(f"- `{g.knob}`: {g.sentence} {g.ask}" for g in h.gaps), ""]
    lines += [
        f"Approved by {who}: {ap.rationale}",
        "",
        f"Package digest `{h.digest}`; research `{h.provenance.research_sha[:12]}`, profiles "
        f"`{h.provenance.profiles_sha[:12]}`.",
        "",
        "`CircuitParams.from_bundle_meta(json.load(open(path)))` reads the `circuits` values.",
    ]
    return "\n".join(lines)


def open_pr(cfg: Settings, h: Handoff, ap: Approval, text: str) -> str:
    token = cfg.circuits_token or cfg.github_token
    if not token or not cfg.circuits_repo:
        raise GitHubError("RESEARCH_CIRCUITS_REPO and a token are needed to open a PR")
    branch = f"research/handoff-{h.clade}-{h.digest[:12]}"
    file = PR_FILE.format(clade=h.clade)
    tmp = Path(tempfile.mkdtemp(prefix="research-handoff-"))
    try:
        repo = tmp / "circuits"
        _git(["clone", "--quiet", "--depth", "1", _remote(cfg, token), str(repo)], tmp, token)
        _git(["checkout", "--quiet", "-b", branch], repo, None)
        (repo / file).parent.mkdir(parents=True, exist_ok=True)
        (repo / file).write_text(text)
        _git(["add", file], repo, None)
        _git(
            [
                "-c",
                "user.name=senseai-research",
                "-c",
                "user.email=senseai-research@users.noreply.github.com",
                "commit",
                "--quiet",
                "-m",
                f"research: {h.clade} evidence handoff {h.digest[:12]}\n\n"
                f"Approved by {ap.by.removeprefix('user:')} in senseai-research.",
            ],
            repo,
            None,
        )
        _git(["push", "--quiet", _remote(cfg, token), f"HEAD:refs/heads/{branch}"], repo, token)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    with httpx.Client(transport=github.transport, timeout=30.0) as client:
        r = client.post(
            f"{cfg.github_api}/repos/{cfg.circuits_repo}/pulls",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            json={
                "title": f"research: {h.clade} evidence handoff",
                "head": branch,
                "base": cfg.circuits_base,
                "body": pr_body(h, ap),
            },
        )
    if r.status_code >= 300:
        raise GitHubError(f"GitHub refused the pull request ({r.status_code}): {r.text[:300]}")
    url = str(r.json().get("html_url") or "")
    if not url:
        raise GitHubError("GitHub answered without a pull request URL")
    return url


def release(
    store: Store, cfg: Settings, action: PendingAction, ap: Approval, h: Handoff
) -> HandoffRelease:
    """Write, optionally push, and record an approved package."""
    if ap.decision != "approve" or not ap.by.startswith("user:") or ap.target_id != action.id:
        raise LedgerError("a handoff is released only with a person's approval (N1)")
    text = h.text()
    out = outbox(cfg)
    out.mkdir(parents=True, exist_ok=True)
    f = out / f"{h.clade}.{h.digest[:12]}.json"
    f.write_text(text)
    pr_url, pr_error = None, None
    if cfg.circuits_repo:
        try:
            pr_url = open_pr(cfg, h, ap, text)
        except GitHubError as e:  # the package is still released and downloadable
            pr_error = str(e)
    rel = HandoffRelease(
        id=new_id("hnd"),
        created_by=ap.by,
        clade=h.clade,
        digest=h.digest,
        action_id=action.id,
        approval_id=ap.id,
        by=ap.by,
        rationale=ap.rationale,
        file=str(f.relative_to(cfg.data)),
        circuits=dict(h.circuits),
        pr_url=pr_url,
        pr_error=pr_error,
    )
    store.append("handoff.released", ap.by, {"handoff": rel.model_dump()})
    return rel


def latest(store: Store, clade: str) -> HandoffRelease | None:
    rels = [r for r in store.all("handoff", HandoffRelease) if r.clade == clade.lower()]
    return max(rels, key=lambda r: r.id) if rels else None


__all__ = ["PR_FILE", "latest", "open_pr", "outbox", "pr_body", "release"]
