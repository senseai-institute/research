# SPDX-License-Identifier: Apache-2.0
"""Open the library PR on profiles (spec D6). The only code that writes to a profiles repo.

It runs only for a proposal whose Approval row exists (checked here, not only by the caller).
The base checkout is not touched: a ``git clone --shared`` copy gets branch
``research/<proposal-id>`` with one commit, which is pushed with ``GITHUB_TOKEN`` (scoped to
profiles only). The PR is then opened with ``POST /repos/{owner}/{repo}/pulls`` over ``httpx``.
No ``gh`` CLI is needed.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

import httpx

from senseai_research.config import Settings
from senseai_research.ledger.models import Approval, PendingAction, Proposal
from senseai_research.ledger.store import Store

#: Tests swap this for a fake GitHub (``httpx.MockTransport``).
transport: httpx.BaseTransport | None = None


class GitHubError(RuntimeError):
    pass


def _redact(text: str, token: str | None) -> str:
    return text.replace(token, "***") if token else text


def _git(args: list[str], cwd: Path, token: str | None) -> str:
    r = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=300, check=False
    )
    if r.returncode != 0:
        raise GitHubError(f"git {args[0]} failed: {_redact(r.stderr.strip(), token)}")
    return r.stdout.strip()


def require_approval(store: Store, proposal: Proposal) -> Approval:
    """The Approval row behind a proposal, or an error. Nothing is pushed without one (N1)."""
    if not proposal.action_id or store.get("action", proposal.action_id, PendingAction) is None:
        raise GitHubError("no Approval row for this proposal; nothing was pushed (N1)")
    for ap in store.all("approval", Approval):
        if ap.target_id == proposal.action_id and ap.target_kind == "proposal":
            if ap.decision == "approve" and ap.by.startswith("user:"):
                return ap
            raise GitHubError("the proposal was rejected; nothing was pushed (N1)")
    raise GitHubError("no Approval row for this proposal; nothing was pushed (N1)")


def remote_url(cfg: Settings) -> str:
    if cfg.github_remote:
        return cfg.github_remote
    return f"https://x-access-token:{cfg.github_token}@github.com/{cfg.github_repo}.git"


def open_pr(
    store: Store, cfg: Settings, proposal: Proposal, *, file: str, new_text: str, body: str
) -> str:
    """Push ``research/<proposal-id>`` with the patched file and open the PR; returns its URL."""
    if not cfg.github_token:
        raise GitHubError("GITHUB_TOKEN is not set")
    if cfg.profiles_repo is None:
        raise GitHubError("SENSEAI_PROFILES_REPO is not set")
    ap = require_approval(store, proposal)
    branch = f"research/{proposal.id}"
    tmp = Path(tempfile.mkdtemp(prefix="research-pr-"))
    try:
        repo = tmp / "profiles"
        _git(["clone", "--quiet", "--shared", str(cfg.profiles_repo), str(repo)], tmp, None)
        _git(["checkout", "--quiet", "-b", branch], repo, None)
        (repo / file).write_text(new_text)
        who = ap.by.removeprefix("user:")
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
                f"{proposal.title}\n\nApproved by {who} in senseai-research ({proposal.id}).",
            ],
            repo,
            None,
        )
        _git(
            ["push", "--quiet", remote_url(cfg), f"HEAD:refs/heads/{branch}"],
            repo,
            cfg.github_token,
        )
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    with httpx.Client(transport=transport, timeout=30.0) as client:
        r = client.post(
            f"{cfg.github_api}/repos/{cfg.github_repo}/pulls",
            headers={
                "Authorization": f"Bearer {cfg.github_token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            json={"title": proposal.title, "head": branch, "base": cfg.github_base, "body": body},
        )
    if r.status_code >= 300:
        raise GitHubError(f"GitHub refused the pull request ({r.status_code}): {r.text[:300]}")
    url = str(r.json().get("html_url") or "")
    if not url:
        raise GitHubError("GitHub answered without a pull request URL")
    return url


__all__ = ["GitHubError", "open_pr", "remote_url", "require_approval", "transport"]
