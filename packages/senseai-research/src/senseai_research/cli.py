# SPDX-License-Identifier: Apache-2.0
"""``senseai-research``: the command line.

Commands: ``import``, ``export``, ``schema``, ``backup``, ``claims``, ``impact``,
``experiment``, ``pending``, ``approve``, ``reject``, ``ask``, ``agent-doc``, ``api-doc``,
``settings``, ``serve``.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

from senseai_research.config import Settings
from senseai_research.ledger.models import Impact
from senseai_research.ledger.store import Store

Command = Callable[[argparse.Namespace, Settings], int]
_COMMANDS: dict[str, tuple[str, Command, Callable[[argparse.ArgumentParser], None]]] = {}


def command(
    name: str, help_: str, args: Callable[[argparse.ArgumentParser], None] = lambda p: None
) -> Callable[[Command], Command]:
    def deco(fn: Command) -> Command:
        _COMMANDS[name] = (help_, fn, args)
        return fn

    return deco


def open_store(cfg: Settings) -> Store:
    return Store(cfg.db_path)


def _profiles(a: argparse.Namespace, cfg: Settings) -> Path:
    p = Path(a.profiles) if getattr(a, "profiles", None) else cfg.profiles_repo
    if p is None:
        raise SystemExit("set SENSEAI_PROFILES_REPO or pass --profiles (a profiles checkout)")
    return p


def _import_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--profiles", help="profiles checkout (default: $SENSEAI_PROFILES_REPO)")
    p.add_argument(
        "--curate-runs", help="curate run states (default: $SENSEAI_DATA/curate-cache/runs)"
    )


@command("import", "import library values, research requests and open questions", _import_args)
def cmd_import(a: argparse.Namespace, cfg: Settings) -> int:
    from senseai_research import literature
    from senseai_research.ledger.importers import import_library

    runs = Path(a.curate_runs) if a.curate_runs else literature.run_state_dir()
    store = open_store(cfg)
    res = import_library(store, _profiles(a, cfg), runs_dir=runs if runs.exists() else None)
    print(res.summary())
    return 0


def _export_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--out", required=True, help="directory for the YAML export")


@command("export", "write the ledger as reviewable YAML", _export_args)
def cmd_export(a: argparse.Namespace, cfg: Settings) -> int:
    from senseai_research.ledger.export import export

    files = export(open_store(cfg), Path(a.out))
    print(f"wrote {len(files)} files under {a.out}")
    return 0


def _schema_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--out", default="schema", help="directory for the JSON Schema files")


@command("schema", "write the JSON Schema of every ledger record", _schema_args)
def cmd_schema(a: argparse.Namespace, cfg: Settings) -> int:
    from senseai_research.ledger import models

    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    names = [
        "Source",
        "Passage",
        "Claim",
        "ClaimRevision",
        "Hypothesis",
        "Experiment",
        "Proposal",
        "Approval",
        "PendingAction",
        "Impact",
        "Event",
    ]
    for n in names:
        model = getattr(models, n)
        (out / f"{n.lower()}.schema.json").write_text(
            json.dumps(model.model_json_schema(), indent=1, sort_keys=True) + "\n"
        )
    print(f"wrote {len(names)} schemas under {out}")
    return 0


def _backup_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--out", help="backup file (default: <data>/backups/ledger-<seq>.sqlite)")
    p.add_argument("--s3", help="also copy the backup and the curate cache to s3://bucket/prefix")


@command("backup", "copy the ledger (consistent snapshot), optionally to S3", _backup_args)
def cmd_backup(a: argparse.Namespace, cfg: Settings) -> int:
    store = open_store(cfg)
    seq = store.events()[-1].seq if store.events() else 0
    out = Path(a.out) if a.out else cfg.data / "backups" / f"ledger-{seq:08d}.sqlite"
    out.parent.mkdir(parents=True, exist_ok=True)
    dst = sqlite3.connect(out)
    with dst, store._lock:
        store._conn.backup(dst)
    dst.close()
    print(f"backup: {out}")
    s3 = a.s3 or cfg.curate_s3
    if s3:
        from senseai_research.curate.cache import sync_s3

        n = sync_s3(out.parent, s3.rstrip("/") + "/ledger", direction="push")
        n += sync_s3(cfg.cache_dir, s3.rstrip("/") + "/curate-cache", direction="push")
        print(f"copied {n} file(s) to {s3}")
    return 0


def _claims_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--status", help="only claims with this status")
    p.add_argument("--path", help="only claims whose library path starts with this")


@command("claims", "list claims in plain words", _claims_args)
def cmd_claims(a: argparse.Namespace, cfg: Settings) -> int:
    for c in open_store(cfg).claims():
        if a.status and c.status != a.status:
            continue
        if a.path and not c.library_path.startswith(a.path):
            continue
        print(f"{c.status:<11} {c.library_path} = {c.quantity.text()}")
    return 0


def _change_args(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--path", required=True, help="library path, e.g. felidae.senses.vision.detection_threshold"
    )
    p.add_argument("--value", type=float, help="new value (in the library's unit)")
    p.add_argument("--low", type=float, help="new range: low end")
    p.add_argument("--high", type=float, help="new range: high end")
    p.add_argument("--unit", help="unit (must be the library's; default: the library's)")


def _quantity(a: argparse.Namespace) -> dict[str, object]:
    if a.value is None and (a.low is None or a.high is None):
        raise SystemExit("give --value, or --low and --high")
    q: dict[str, object] = {"value": a.value, "low": a.low, "high": a.high}
    if a.unit:
        q["unit"] = a.unit
    return q


def print_impact(imp: Impact) -> None:
    nq = imp.new_quantity
    new = nq.get("value") if nq.get("value") is not None else [nq.get("low"), nq.get("high")]
    print(f"Changing {imp.library_path}: {imp.old_quantity.get('value')} -> {new}")
    print("Read by: " + (", ".join(imp.consumers) or "no profile field"))
    for s in imp.sentences:
        print(f"  - {s}")
    if imp.field_summaries:
        print("\n  field                              point            old       new      change")
        for r in imp.profile_rows:
            d = "" if r.delta_pct is None else f"{r.delta_pct:+.0f}%"
            name = f"{r.profile}.{r.section}.{r.field}"
            print(f"  {name:<34} {r.point:<14} {r.old!s:>9.6} {r.new!s:>9.6} {d:>8}")
    print(f"(impact record {imp.id})")


@command("impact", "what a library change does to the profiles and the world prior", _change_args)
def cmd_impact(a: argparse.Namespace, cfg: Settings) -> int:
    from senseai_research.impact import preview

    imp = preview(open_store(cfg), cfg, a.path, _quantity(a), actor="user:cli")
    print_impact(imp)
    return 0


def _experiment_args(p: argparse.ArgumentParser) -> None:
    _change_args(p)
    p.add_argument("--sequences", required=True, help="comma list of recording names (or folders)")
    p.add_argument("--arms", default="panther_prior,null_profile", help="comma list of bench arms")
    p.add_argument("--max-seconds", type=float, help="only the first N seconds of each recording")


@command(
    "experiment", "run bench on the patched library against a cached baseline", _experiment_args
)
def cmd_experiment(a: argparse.Namespace, cfg: Settings) -> int:
    from senseai_research.experiments import Change, ExperimentSpec, run_experiment
    from senseai_research.experiments.record import compare, table
    from senseai_research.impact import preview

    store = open_store(cfg)
    q = _quantity(a)
    print_impact(preview(store, cfg, a.path, q, actor="user:cli"))
    spec = ExperimentSpec(
        sequences=[s.strip() for s in a.sequences.split(",") if s.strip()],
        arms=[s.strip() for s in a.arms.split(",") if s.strip()],
        changes=[Change(a.path, q)],
        max_seconds=a.max_seconds,
    )
    last = [""]

    def say(p: dict[str, object]) -> None:
        msg = str(p.get("message", ""))
        if msg and msg != last[0]:
            print(f"  ... {msg}", flush=True)
            last[0] = msg

    base, pat = run_experiment(store, cfg, spec, actor="user:cli", progress=say)
    print(f"\nExperiment {pat.id} (baseline {base.id})")
    print(table(compare(base, pat)))
    return 0 if pat.status == "done" else 1


@command("pending", "list changes waiting for a person to approve or reject")
def cmd_pending(a: argparse.Namespace, cfg: Settings) -> int:
    from senseai_research.actions import pending

    items = pending(open_store(cfg))
    if not items:
        print("Nothing is waiting for approval.")
    for act in items:
        print(f"{act.id}  {act.card.get('title', act.kind)}")
        print(f"    {act.card.get('summary', '')}")
        for r in act.card.get("rows", []):
            print(f"    {r['label']}: {r['before']} -> {r['after']}")
        if act.card.get("quote"):
            print(f"    quote: “{act.card['quote']}”")
    return 0


def _decide_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("action_id")
    p.add_argument("--rationale", required=True, help="why (at least one sentence)")
    p.add_argument("--user", default=None, help="your name (default: $USER)")


def _decide(a: argparse.Namespace, cfg: Settings, decision: str) -> int:
    import getpass

    from senseai_research import actions

    store = open_store(cfg)
    by = f"user:{a.user or getpass.getuser()}"
    if decision == "approve":
        out = actions.approve(store, cfg, a.action_id, by=by, rationale=a.rationale)
    else:
        out = actions.reject(store, a.action_id, by=by, rationale=a.rationale)
    print(out.message)
    return 0 if out.action.status in ("approved", "rejected") else 1


@command("approve", "approve a pending change (records who and why)", _decide_args)
def cmd_approve(a: argparse.Namespace, cfg: Settings) -> int:
    return _decide(a, cfg, "approve")


@command("reject", "reject a pending change (records who and why)", _decide_args)
def cmd_reject(a: argparse.Namespace, cfg: Settings) -> int:
    return _decide(a, cfg, "reject")


def _agent_doc_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--out", default="docs/AGENT.md")


@command(
    "agent-doc",
    "write docs/AGENT.md (tools, model call, guardrails) from the code",
    _agent_doc_args,
)
def cmd_agent_doc(a: argparse.Namespace, cfg: Settings) -> int:
    from senseai_research.agent.doc import render

    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(render())
    print(f"wrote {a.out}")
    return 0


def _api_doc_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--out", default="docs/APIS.md")


@command(
    "api-doc",
    "write docs/APIS.md (outside services and their API keys) from the code",
    _api_doc_args,
)
def cmd_api_doc(a: argparse.Namespace, cfg: Settings) -> int:
    from senseai_research.integrations import render_doc

    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(render_doc())
    print(f"wrote {a.out}")
    return 0


def _settings_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--check", action="store_true", help="also test each key against its service")
    p.add_argument("--env", action="store_true", help="print the variables as a template")


STATE_LABEL = {
    "set": "set",
    "partial": "partly set",
    "not_set": "not set",
    "no_key": "no key needed",
}


@command("settings", "list outside services and whether their keys are set", _settings_args)
def cmd_settings(a: argparse.Namespace, cfg: Settings) -> int:
    from senseai_research import integrations

    if a.env:
        print(integrations.env_template(), end="")
        return 0
    missing_required = False
    for g in integrations.GROUPS:
        print(g)
        for row in integrations.status():
            if row["group"] != g:
                continue
            names = ", ".join(v["name"] for v in row["vars"]) or "-"
            line = f"  {row['name']:<34} {row['need']:<12} {STATE_LABEL[row['state']]:<14} {names}"
            if a.check and row["checkable"]:
                r = integrations.check(row["id"])
                line += f"\n      {'ok' if r.ok else 'problem'}: {r.detail}"
            print(line)
            missing_required |= row["need"] == "required" and row["state"] != "set"
    return 1 if missing_required else 0


def _ask_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--message", "-m", help="send one message and exit (otherwise interactive)")
    p.add_argument("--conversation", help="continue this conversation id")
    p.add_argument("--user", default=None, help="your name (default: $USER)")


def _printer() -> Callable[[str, dict[str, object]], None]:
    state = {"in_text": False}

    def emit(kind: str, d: dict[str, object]) -> None:
        if kind == "text_delta":
            print(d["text"], end="", flush=True)
            state["in_text"] = True
            return
        if state["in_text"]:
            print()
            state["in_text"] = False
        if kind == "tool_call":
            print(f"  [{d['label']}]", flush=True)
        elif kind == "tool_result_summary" and not d.get("ok"):
            print(f"  [problem: {d['summary']}]", flush=True)
        elif kind == "job_progress":
            print(f"  [{d.get('message', '')}]", flush=True)
        elif kind == "pending_action":
            act = d["action"]
            assert isinstance(act, dict)
            card = act["card"]
            print(f"\n  === Waiting for you: {card.get('title')} ({act['id']}) ===")
            for r in card.get("rows", []):
                print(f"  {r['label']}: {r['before']} -> {r['after']}")
            if card.get("quote"):
                print(f"  Quote: \u201c{card['quote']}\u201d")
            print(f"  Approve with  :approve {act['id']} <your reason>")
            print(f"  Reject with   :reject {act['id']} <your reason>\n")
        elif kind == "error":
            print(f"  [note: {d.get('message')}]", flush=True)
        elif kind == "done":
            u = d.get("usage") or {}
            assert isinstance(u, dict)
            print(f"\n  (cost of this turn: ${u.get('cost_usd', 0)})")

    return emit


@command("ask", "chat with the research assistant in the terminal", _ask_args)
def cmd_ask(a: argparse.Namespace, cfg: Settings) -> int:
    import getpass

    from senseai_research.agent import Agent

    store = open_store(cfg)
    agent = Agent(store, cfg)
    user = f"user:{a.user or getpass.getuser()}"
    conv = a.conversation or agent.new_conversation(user)
    emit = _printer()
    if a.message:
        agent.send(conv, a.message, emit=emit, user=user)
        print(f"\n(conversation {conv})")
        return 0
    print(f"Conversation {conv}. Type your question; :approve / :reject <id> <reason>; :quit.")
    while True:
        try:
            line = input("\nyou> ").strip()
        except EOFError:
            return 0
        if not line:
            continue
        if line in (":quit", ":q"):
            return 0
        if line.startswith((":approve ", ":reject ")):
            cmd, _, rest = line.partition(" ")
            action_id, _, why = rest.partition(" ")
            try:
                out = agent.decide(action_id, cmd[1:], user=user, rationale=why)
            except ValueError as e:
                print(f"  {e}")
                continue
            print(f"  {out.message}")
            continue
        agent.send(conv, line, emit=emit, user=user)


def _serve_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--port", type=int, default=None, help="default: $RESEARCH_PORT or 8100")


@command(
    "serve", "run the research server on 127.0.0.1 (the viewer proxies /api/research)", _serve_args
)
def cmd_serve(a: argparse.Namespace, cfg: Settings) -> int:
    import uvicorn

    from senseai_research.server.app import create_app

    port = a.port or cfg.port
    uvicorn.run(create_app(cfg), host="127.0.0.1", port=port, log_level="info")
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="senseai-research", description=__doc__)
    ap.add_argument("--data", help="data directory (default: $RESEARCH_DATA)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, (help_, fn, args) in _COMMANDS.items():
        p = sub.add_parser(name, help=help_)
        args(p)
        p.set_defaults(fn=fn)
    return ap


def main(argv: Sequence[str] | None = None) -> int:
    a = build_parser().parse_args(argv)
    cfg = Settings.from_env()
    if a.data:
        cfg = cfg.with_(data=Path(a.data))
    fn: Command = a.fn
    return fn(a, cfg)


if __name__ == "__main__":
    sys.exit(main())
