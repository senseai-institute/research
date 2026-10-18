# SPDX-License-Identifier: Apache-2.0
"""The research server: ``/api/research/*`` behind the viewer's proxy (spec §8).

It binds to ``127.0.0.1:$RESEARCH_PORT``. The viewer proxy authenticates the person and
forwards their name in ``X-SenseAI-User``; this server believes that header only from the
loopback interface and refuses every request from anywhere else. Every Approval stores that
user.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from senseai_research import __version__, views
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
    health_cache: dict[str, Any] = {"t": 0.0, "model": (False, "not checked")}

    app = FastAPI(title="senseai-research", version=__version__)

    @app.middleware("http")
    async def loopback_only(request: Request, call_next: Any) -> Any:
        host = request.client.host if request.client else ""
        if host not in trusted:
            return JSONResponse(
                {"detail": "the research server answers only the viewer on this machine"}, 403
            )
        return await call_next(request)

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
        return {"id": agent.new_conversation(person(x_senseai_user))}

    @app.get("/api/research/conversations")
    def conversations() -> list[dict[str, Any]]:
        rows = store.list_raw("conversation")
        return sorted(rows, key=lambda r: r["id"], reverse=True)[:30]

    @app.get("/api/research/conversations/{cid}")
    def transcript(cid: str) -> dict[str, Any]:
        if not agent.transcript.exists(cid):
            raise HTTPException(404, f"there is no conversation {cid!r}")
        return {"id": cid, "items": agent.view(cid)}

    @app.post("/api/research/conversations/{cid}/messages")
    async def send(
        cid: str, body: MessageBody, x_senseai_user: str | None = Header(None)
    ) -> EventSourceResponse:
        user = person(x_senseai_user)
        if not agent.transcript.exists(cid):
            raise HTTPException(404, f"there is no conversation {cid!r}")
        with busy_lock:
            if cid in busy:
                raise HTTPException(409, "the assistant is still answering in this conversation")
            busy.add(cid)

        def run(emit: Any) -> None:
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
        return [a.model_dump() for a in store.all("action", PendingAction) if a.status == "pending"]

    # -- the ledger ----------------------------------------------------------------------

    @app.get("/api/research/start")
    def start() -> dict[str, Any]:
        return {
            "open_questions": views.open_questions(store),
            "least_verified": views.least_verified(store, cfg),
        }

    @app.get("/api/research/claims")
    def claims(status: str | None = None, path: str | None = None) -> list[dict[str, Any]]:
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
        must("passage", passage_id, Passage)
        return _passage_view(store, passage_id)

    @app.get("/api/research/hypotheses")
    def hypotheses() -> list[dict[str, Any]]:
        return [h.model_dump() for h in store.all("hypothesis", Hypothesis)]

    @app.get("/api/research/experiments/{exp_id}")
    def experiment(exp_id: str) -> dict[str, Any]:
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
        imp: Impact = must("impact", impact_id, Impact)
        return imp.model_dump()

    @app.get("/api/research/proposals")
    def proposals() -> list[dict[str, Any]]:
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
        if time.monotonic() - health_cache["t"] > 60:
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
