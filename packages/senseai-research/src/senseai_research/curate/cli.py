# SPDX-License-Identifier: Apache-2.0
"""``senseai-library``: draft, review and approve library entries from the literature."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from senseai_research.curate import assemble as asm
from senseai_research.curate import pipeline as pl
from senseai_research.curate import profiles_root
from senseai_research.curate.cache import CachedClient, ModelCache, data_root, sync_s3

DEFAULT_ROOT = profiles_root()


def _client(args: argparse.Namespace) -> CachedClient:
    email = os.environ.get("SENSEAI_CONTACT_EMAIL") or os.environ.get("UNPAYWALL_EMAIL")
    return CachedClient(
        data_root() / "curate-cache",
        offline=bool(args.offline or os.environ.get("SENSEAI_CURATE_OFFLINE")),
        email=email,
    )


def _library(args: argparse.Namespace) -> Path:
    return Path(args.root) / "library"


def _state(args: argparse.Namespace) -> pl.RunState:
    senses = [s for s in (args.senses or "").split(",") if s]
    st = pl.RunState.load(args.clade, senses)
    if not st.senses:
        entry = asm.load_entry_data(_library(args) / f"{args.clade}.yaml")
        st.senses = [s["id"] for s in entry.get("senses", [])] if entry else ["vision"]
    return st


def cmd_taxonomy(args: argparse.Namespace) -> int:
    st = _state(args)
    t = pl.step_taxonomy(_client(args), st, args.name)
    st.save()
    print(json.dumps(t.to_dict(), indent=1, default=str))
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    st = _state(args)
    client = _client(args)
    if st.taxon is None:
        pl.step_taxonomy(client, st)
    res = pl.step_search(
        client,
        st,
        sources=tuple(args.sources.split(",")),
        max_queries_per_path=args.max_queries,
        max_papers=args.max_papers,
    )
    st.cache_digest = client.digest()
    st.save()
    print(f"{args.clade}: {len(res.papers)} unique papers from {len(res.queries)} queries")
    for e in res.errors:
        print(f"  warning: {e}", file=sys.stderr)
    return 0


def cmd_fetch(args: argparse.Namespace) -> int:
    st = _state(args)
    client = _client(args)
    texts = pl.step_fetch(client, st)
    st.cache_digest = client.digest()
    st.save()
    full = sum(1 for t in texts.values() if t.fulltext)
    abs_only = sum(1 for t in texts.values() if not t.fulltext and t.text)
    print(
        f"{args.clade}: {full} full text, {abs_only} abstract only, "
        f"{len(texts) - full - abs_only} no text"
    )
    return 0


def cmd_extract(args: argparse.Namespace) -> int:
    st = _state(args)
    exs = pl.step_extract(st, cache=ModelCache(data_root() / "curate-cache"))
    st.save()
    n = sum(len(e.claims) for e in exs.values())
    rej = sum(len(e.rejected) for e in exs.values())
    statuses: dict[str, int] = {}
    for e in exs.values():
        statuses[e.status] = statuses.get(e.status, 0) + 1
    print(f"{args.clade}: {n} claims kept, {rej} rejected by the quote check; {statuses}")
    return 0


def cmd_vet(args: argparse.Namespace) -> int:
    st = _state(args)
    client = _client(args)
    vs = pl.step_vet(client, st)
    st.cache_digest = client.digest()
    st.save()
    flagged = {d: v.flags for d, v in vs.items() if v.flags}
    print(f"{args.clade}: {len(vs)} papers vetted, {len(flagged)} flagged")
    for d, f in flagged.items():
        print(f"  {d}: {', '.join(f)}")
    return 0


def cmd_reconcile(args: argparse.Namespace) -> int:
    st = _state(args)
    rec = pl.step_reconcile(st)
    st.save()
    print(f"{args.clade}: {rec.counts()}; {len(rec.unresolved_paths)} paths empty")
    return 0


def cmd_assemble(args: argparse.Namespace) -> int:
    st = _state(args)
    _data, path = pl.step_assemble(st, _library(args))
    st.save()
    sheet = pl.step_review(st, _library(args))
    print(f"wrote {path} and {sheet}")
    return 0


def cmd_draft(args: argparse.Namespace) -> int:
    st = _state(args)
    client = _client(args)
    pl.step_taxonomy(client, st, args.name)
    st.save()
    pl.step_search(
        client,
        st,
        sources=tuple(args.sources.split(",")),
        max_queries_per_path=args.max_queries,
        max_papers=args.max_papers,
    )
    st.save()
    pl.step_fetch(client, st)
    st.save()
    pl.step_extract(st, cache=ModelCache(data_root() / "curate-cache"))
    st.save()
    pl.step_vet(client, st)
    pl.step_reconcile(st)
    st.cache_digest = client.digest()
    _data, path = pl.step_assemble(st, _library(args))
    sheet = pl.step_review(st, _library(args))
    st.save()
    print(json.dumps(pl.summary(st), indent=1))
    print(f"wrote {path} and {sheet}")
    return 0


def cmd_review(args: argparse.Namespace) -> int:
    st = _state(args)
    sheet = pl.step_review(st, _library(args))
    print(f"wrote {sheet}")
    return 0


def cmd_approve(args: argparse.Namespace) -> int:
    from senseai_research.curate.review import approve

    lib = _library(args)
    try:
        out = approve(
            lib / f"{args.clade}.yaml", lib / "review" / f"{args.clade}.md", reviewer=args.reviewer
        )
    except (ValueError, FileNotFoundError) as e:
        print(f"refused: {e}", file=sys.stderr)
        return 1
    print(f"{args.clade}: reviewed by {out['reviewed_by']}, {out['decisions']} decisions applied")
    for q in out["open_questions"]:
        print(f"  open question: {q}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    runs = sorted((data_root() / "curate-cache" / "runs").glob("*.json"))
    if args.clade:
        runs = [r for r in runs if r.stem == args.clade]
    for r in runs:
        print(json.dumps(pl.summary(pl.RunState.load(r.stem)), indent=1))
    if not runs:
        print("no runs yet")
    return 0


def cmd_cache(args: argparse.Namespace) -> int:
    n = sync_s3(data_root() / "curate-cache", args.s3_url, direction=args.direction)
    print(f"{args.direction}: {n} files")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="senseai-library", description=__doc__)
    p.add_argument("--root", default=str(DEFAULT_ROOT), help="repository root (library/)")
    p.add_argument("--offline", action="store_true", help="serve only from the cache")
    sub = p.add_subparsers(dest="cmd", required=True)

    def clade_cmd(name: str, help_: str) -> argparse.ArgumentParser:
        s = sub.add_parser(name, help=help_)
        s.add_argument("clade", help="library key, e.g. felidae")
        s.add_argument("--senses", default=None, help="comma list; default from the entry")
        return s

    s = clade_cmd("taxonomy", "resolve the clade (GBIF, OTOL, COL, IUCN, Wikidata)")
    s.add_argument("--name", default=None)
    s.set_defaults(fn=cmd_taxonomy)
    for name, fn, help_ in (
        ("search", cmd_search, "literature search per catalogue path"),
        ("draft", cmd_draft, "run every step and write the YAML plus review sheet"),
    ):
        s = clade_cmd(name, help_)
        s.add_argument("--sources", default="openalex,europepmc,pubmed,s2")
        s.add_argument("--max-queries", type=int, default=3)
        s.add_argument("--max-papers", type=int, default=60)
        s.add_argument("--name", default=None, help="taxon name if not the capitalised key")
        s.set_defaults(fn=fn)
    clade_cmd("fetch", "fetch open-access text").set_defaults(fn=cmd_fetch)
    clade_cmd("extract", "extract claims (Anthropic API, cached)").set_defaults(fn=cmd_extract)
    clade_cmd("vet", "retractions, PubPeer, venue, citation intents").set_defaults(fn=cmd_vet)
    clade_cmd("reconcile", "agreement and confidence per path").set_defaults(fn=cmd_reconcile)
    clade_cmd("assemble", "write the draft YAML and sheet from the run state").set_defaults(
        fn=cmd_assemble
    )
    clade_cmd("review", "regenerate the review sheet").set_defaults(fn=cmd_review)
    s = clade_cmd("approve", "apply reviewer decisions; flips review_status")
    s.add_argument("--reviewer", required=True, help="name as it should appear in contributors")
    s.set_defaults(fn=cmd_approve)
    s = sub.add_parser("status", help="summary of runs on this machine")
    s.add_argument("clade", nargs="?", default=None)
    s.set_defaults(fn=cmd_status)
    s = sub.add_parser("cache", help="mirror the cache to or from S3")
    s.add_argument("direction", choices=["push", "pull"])
    s.add_argument("s3_url", help="s3://bucket/prefix")
    s.set_defaults(fn=cmd_cache)
    return p


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    rc: int = args.fn(args)
    return rc


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
