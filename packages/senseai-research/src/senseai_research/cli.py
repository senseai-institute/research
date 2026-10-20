# SPDX-License-Identifier: Apache-2.0
"""``senseai-research``: the command line.

Commands: ``import``, ``export``, ``schema``, ``backup``, ``claims``, ``impact``.
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
    with dst:
        store._conn.backup(dst)
    dst.close()
    print(f"backup: {out}")
    s3 = a.s3 or cfg.curate_s3
    if s3:
        from senseai_profiles.curate.cache import sync_s3

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
