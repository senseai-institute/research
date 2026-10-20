# SPDX-License-Identifier: Apache-2.0
"""The research server: ``/api/research/*`` behind the viewer's proxy (spec §8).

It binds to ``127.0.0.1:$RESEARCH_PORT`` by default. A front end (Studio, or the viewer)
authenticates the person and forwards their name in ``X-SenseAI-User``; this server believes
that header only from a trusted caller and refuses everyone else. A caller is trusted when it
is on this machine (loopback), or when it sends ``X-SenseAI-Proxy-Token`` equal to
``RESEARCH_PROXY_TOKEN``: that is how Studio reaches it on Railway. Every Approval stores the
forwarded user. ``/healthz`` is open (it says only "ok") for the platform's health check.
"""

from __future__ import annotations

import secrets
import threading
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from senseai_research import __version__, integrations, views
from senseai_research.actions import ActionError
from senseai_research.agent import Agent
from senseai_research.config import Settings
from senseai_research.experiments.jobs import ExperimentQueue
from senseai_research.experiments.record import compare
from senseai_research.impact import git_sha
from senseai_research.ledger.models import (
    Experiment,
    Hypothesis,
    Impact,
    Passage,
    PendingAction,
    Proposal,
    Source,
)
from senseai_research.ledger.store import LedgerError, Store
from senseai_research.server.sse import stream_turn

LOOPBACK = ("127.0.0.1", "::1")
SETTINGS_WHERE = (
    "Keys are set as environment variables on the research server, never on this page. "
    "On Railway: the research service, Variables tab (Railway redeploys when you save). "
    "On the EC2 box: ~/senseai/research.env, then research-ctl.sh restart."
)


class MessageBody(BaseModel):
    text: str = Field(min_length=1, max_length=20000)
    focus: dict[str, str] | None = None


class DecisionBody(BaseModel):
    rationale: str = Field(min_length=1, max_length=5000)


def create_app(
    cfg: Settings | None = None,
    *,
    store: Store | None = None,
    agent: Agent | None = None,
    trusted_hosts: Iterable[str] = LOOPBACK,
    model_check: Callable[[], tuple[bool, str]] | None = None,
    check_http: httpx.Client | None = None,
) -> FastAPI:
    cfg = cfg or Settings.from_env()
    store = store or Store(cfg.db_path)
    if agent is None:
        queue = ExperimentQueue(store, cfg)
        agent = Agent(store, cfg, queue=queue)
    else:
        queue = agent.loop.queue or ExperimentQueue(store, cfg)
        agent.loop.queue = queue
    trusted = set(trusted_hosts)
    busy: set[str] = set()
    busy_lock = threading.Lock()
    health_cache: dict[str, Any] = {"t": None, "model": (False, "not checked")}

    app = FastAPI(
        title="senseai-research",
        version=__version__,
        description="The research backend. Chat events and headers: docs/API.md.",
        openapi_url="/api/research/openapi.json",
        docs_url="/api/research/docs",
        redoc_url=None,
    )

    @app.middleware("http")
    async def trusted_callers_only(request: Request, call_next: Any) -> Any:
        if request.url.path == "/healthz":
            return await call_next(request)
        host = request.client.host if request.client else ""
        given = request.headers.get("x-senseai-proxy-token", "")
        by_token = bool(cfg.proxy_token) and secrets.compare_digest(
            given.encode(), (cfg.proxy_token or "").encode()
        )
        if host not in trusted and not by_token:
            return JSONResponse(
                {
                    "detail": "the research server answers only its front end (Studio or the "
                    "viewer): on this machine, or with the shared RESEARCH_PROXY_TOKEN"
                },
                403,
            )
        return await call_next(request)

    @app.get("/healthz", include_in_schema=False)
    def healthz() -> dict[str, bool]:
        return {"ok": True}

    def person(x_senseai_user: str | None) -> str:
        name = (x_senseai_user or "").strip()
        if not name:
            raise HTTPException(401, "no signed-in user (open this through the viewer)")
        return f"user:{name}"

    def must(kind: str, id_: str, model: type[Any]) -> Any:
        got = store.get(kind, id_, model)
        if got is None:
            raise HTTPException(404, f"there is no {kind} {id_!r}")
        return got

    # -- conversations -------------------------------------------------------------------

    @app.post("/api/research/conversations")
    def new_conversation(x_senseai_user: str | None = Header(None)) -> dict[str, str]:
        """Start a conversation for the signed-in person."""
        return {"id": agent.new_conversation(person(x_senseai_user))}

    @app.get("/api/research/conversations")
    def conversations() -> list[dict[str, Any]]:
        """The 30 most recent conversations."""
        rows = store.list_raw("conversation")
        return sorted(rows, key=lambda r: r["id"], reverse=True)[:30]

    @app.get("/api/research/conversations/{cid}")
    def transcript(cid: str) -> dict[str, Any]:
        """A conversation as the page shows it: messages, tool chips, cards, notes."""
        if not agent.transcript.exists(cid):
            raise HTTPException(404, f"there is no conversation {cid!r}")
        return {"id": cid, "items": agent.view(cid)}

    @app.post("/api/research/conversations/{cid}/messages")
    async def send(
        cid: str, body: MessageBody, x_senseai_user: str | None = Header(None)
    ) -> EventSourceResponse:
        """Send a message; the reply streams as Server-Sent Events (see Chat events)."""
        user = person(x_senseai_user)
        if not agent.transcript.exists(cid):
            raise HTTPException(404, f"there is no conversation {cid!r}")
        with busy_lock:
            if cid in busy:
                raise HTTPException(409, "the assistant is still answering in this conversation")
            busy.add(cid)

        def run(emit: Any) -> None:
            """Send a message; the reply streams as Server-Sent Events (see Chat events)."""
            try:
                agent.send(cid, body.text, emit=emit, focus=body.focus, user=user)
            finally:
                with busy_lock:
                    busy.discard(cid)

        return EventSourceResponse(stream_turn(run), ping=15)

    # -- approvals -----------------------------------------------------------------------

    @app.post("/api/research/actions/{action_id}/{decision}")
    def decide(
        action_id: str, decision: str, body: DecisionBody, x_senseai_user: str | None = Header(None)
    ) -> dict[str, Any]:
        """Approve or reject a pending change, with the person's rationale."""
        if decision not in ("approve", "reject"):
            raise HTTPException(404, "use /approve or /reject")
        user = person(x_senseai_user)
        try:
            out = agent.decide(action_id, decision, user=user, rationale=body.rationale)
        except (ActionError, ValueError, LedgerError) as e:
            raise HTTPException(400, str(e)) from e
        return {
            "action": out.action.model_dump(),
            "approval": out.approval.model_dump(),
            "message": out.message,
        }

    @app.get("/api/research/actions")
    def pending_actions() -> list[dict[str, Any]]:
        """Pending changes waiting for a person."""
        return [a.model_dump() for a in store.all("action", PendingAction) if a.status == "pending"]

    # -- the ledger ----------------------------------------------------------------------

    @app.get("/api/research/start")
    def start() -> dict[str, Any]:
        """The start screen: open questions and the least-verified values."""
        return {
            "open_questions": views.open_questions(store),
            "least_verified": views.least_verified(store, cfg),
        }

    @app.get("/api/research/claims")
    def claims(status: str | None = None, path: str | None = None) -> list[dict[str, Any]]:
        """Claims, filtered by library path prefix or status."""
        out = []
        for c in store.claims():
            if status and c.status != status:
                continue
            if path and not c.library_path.startswith(path):
                continue
            out.append(
                {
                    "claim_id": c.id,
                    "library_path": c.library_path,
                    "value": c.quantity.text(),
                    "status": c.status,
                    "species": c.species,
                    "revision": c.revision,
                }
            )
        return out

    @app.get("/api/research/claims/{claim_id:path}")
    def claim(claim_id: str) -> dict[str, Any]:
        """One claim with its sources, passage and full revision history."""
        try:
            card = views.claim_card(store, claim_id)
        except LedgerError as e:
            raise HTTPException(404, str(e)) from e
        c = card["claim"]
        card["sources"] = [
            _source_view(s) for sid in c["source_ids"] if (s := store.get("source", sid, Source))
        ]
        card["passage"] = _passage_view(store, c["passage_id"]) if c.get("passage_id") else None
        return card

    @app.get("/api/research/passages/{passage_id}")
    def passage(passage_id: str) -> dict[str, Any]:
        """A passage with its context and source."""
        must("passage", passage_id, Passage)
        return _passage_view(store, passage_id)

    @app.get("/api/research/hypotheses")
    def hypotheses() -> list[dict[str, Any]]:
        """Hypotheses and open questions."""
        return [h.model_dump() for h in store.all("hypothesis", Hypothesis)]

    @app.get("/api/research/experiments/{exp_id}")
    def experiment(exp_id: str) -> dict[str, Any]:
        """An experiment: baseline vs patched per arm, chance hit rates, progress."""
        e: Experiment = must("experiment", exp_id, Experiment)
        base = (
            store.get("experiment", e.baseline_experiment_id, Experiment)
            if e.baseline_experiment_id
            else None
        )
        runlogs = (
            sorted(str(p) for p in (Path(e.run_dir) / "runlog").glob("*.h5")) if e.run_dir else []
        )
        job = queue.jobs.get(exp_id)
        return {
            "experiment": e.model_dump(),
            "baseline": base.model_dump() if base else None,
            "comparison": [r.model_dump() for r in compare(base, e)] if e.status == "done" else [],
            "runlogs": runlogs,
            "progress": job.last if job else None,
        }

    @app.get("/api/research/impact/{impact_id}")
    def impact(impact_id: str) -> dict[str, Any]:
        """A stored impact record: consumers, profile diff, reflex, world prior."""
        imp: Impact = must("impact", impact_id, Impact)
        return imp.model_dump()

    @app.get("/api/research/proposals")
    def proposals() -> list[dict[str, Any]]:
        """Library change proposals and their pull requests."""
        out = []
        for p in store.all("proposal", Proposal):
            d = p.model_dump()
            d["downloads"] = (
                {
                    "patch": f"/api/research/proposals/{p.id}/patch",
                    "body": f"/api/research/proposals/{p.id}/body",
                }
                if p.patch_file
                else {}
            )
            out.append(d)
        return out

    @app.get("/api/research/proposals/{pid}/{what}", response_class=PlainTextResponse)
    def proposal_file(pid: str, what: str) -> PlainTextResponse:
        """Download a proposal's patch or pull-request text."""
        p: Proposal = must("proposal", pid, Proposal)
        f = {"patch": p.patch_file, "body": p.pr_body_file}.get(what)
        if not f or not Path(f).exists():
            raise HTTPException(404, "no such download")
        name = f"{pid}.patch" if what == "patch" else f"{pid}.md"
        return PlainTextResponse(
            Path(f).read_text(),
            headers={"Content-Disposition": f'attachment; filename="{name}"'},
        )

    # -- health --------------------------------------------------------------------------

    def check_model() -> tuple[bool, str]:
        if model_check is not None:
            return model_check()
        from senseai_research.agent.client import make_client

        try:
            make_client().with_options(timeout=10.0, max_retries=0).models.retrieve(cfg.model)
            return True, "reachable"
        except Exception as e:
            return False, f"{type(e).__name__}"

    @app.get("/api/research/health")
    def health() -> dict[str, Any]:
        """Version, model reachability, data root, bench, profiles SHA."""
        # monotonic() counts from boot, so a fresh machine can read under 60: check on first use.
        if health_cache["t"] is None or time.monotonic() - health_cache["t"] > 60:
            health_cache.update(t=time.monotonic(), model=check_model())
        ok, why = health_cache["model"]
        return {
            "status": "ok",
            "version": __version__,
            "model": cfg.model,
            "model_reachable": ok,
            "model_detail": why,
            "data": str(cfg.data),
            "data_root": str(cfg.data_root) if cfg.data_root else None,
            "bench_cmd": cfg.bench_cmd,
            "profiles_repo": str(cfg.profiles_repo) if cfg.profiles_repo else None,
            "profiles_sha": git_sha(cfg.profiles_repo) if cfg.profiles_repo else None,
            "github": bool(cfg.github_token),
        }

    # -- settings: outside services and whether their keys are set (values are never sent) --

    checks = integrations.CheckCache()

    @app.get("/api/research/settings")
    def settings_page() -> dict[str, Any]:
        """Outside services and whether their keys are set (never the values)."""
        return {
            "groups": list(integrations.GROUPS),
            "integrations": integrations.status(),
            "where": SETTINGS_WHERE,
        }

    @app.post("/api/research/settings/{iid}/check")
    def check_integration(iid: str, x_senseai_user: str | None = Header(None)) -> dict[str, Any]:
        """Test one service's key with one small request."""
        person(x_senseai_user)
        if iid not in integrations.BY_ID:
            raise HTTPException(404, f"there is no service {iid!r}")
        r, cached = checks.run(iid, http=check_http)
        return {"id": iid, "ok": r.ok, "detail": r.detail, "cached": cached}

    app.state.store = store
    app.state.agent = agent
    app.state.queue = queue
    return app


def _source_view(s: Source) -> dict[str, Any]:
    link = f"https://doi.org/{s.doi}" if s.doi else s.url
    return {
        "source_id": s.id,
        "title": s.title,
        "authors": s.authors,
        "year": s.year,
        "venue": s.venue,
        "doi": s.doi,
        "link": link,
        "full_text": s.fulltext_available,
        "checked": s.checked,
    }


def _passage_view(store: Store, passage_id: str) -> dict[str, Any]:
    p = store.must("passage", passage_id, Passage)
    src = store.must("source", p.source_id, Source)
    return {
        "passage_id": p.id,
        "section": p.section,
        "quote": p.text,
        "before": p.context_before,
        "after": p.context_after,
        "source": _source_view(src),
    }


__all__ = ["LOOPBACK", "create_app"]
