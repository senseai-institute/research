# SPDX-License-Identifier: Apache-2.0
"""Impact preview (spec §5, N4): what a candidate library change does, computed by code.

:func:`preview` writes the library with the new value into a temporary copy, finds the
consumers with ``consumers_by_path``, re-derives every profile on both libraries, runs the world
prior on both files, and stores the result as an :class:`Impact` record. The agent quotes the
stored record; it never recomputes or estimates it.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from senseai_research import libyaml
from senseai_research.config import Settings
from senseai_research.impact import derive as dv
from senseai_research.impact import world as wd
from senseai_research.impact.consumers import ProfilesTree, consumers
from senseai_research.ledger.models import Hypothesis, Impact, WorldRow, new_id
from senseai_research.ledger.store import Store

WORLD_GAP = "this value does not reach the world model yet"


class ImpactError(ValueError):
    pass


def git_sha(path: Path) -> str:
    try:
        out = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
        return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return "not a git checkout"


def patch_text(old: str, library_path: str, new: Mapping[str, Any]) -> str:
    """``old`` (a library file's text) with ``library_path`` set to the quantity ``new``."""
    cur = libyaml.get_quantity(old, library_path)
    unit = new.get("unit") or cur.get("unit")
    if unit != cur.get("unit"):
        raise ImpactError(
            f"the library keeps {library_path} in {cur.get('unit')!r}; give the new value in "
            f"that unit (got {unit!r})"
        )
    changes: dict[str, Any] = {}
    if new.get("value") is not None:
        changes["value"] = float(new["value"])
    elif new.get("low") is not None and new.get("high") is not None:
        changes["value"] = [float(new["low"]), float(new["high"])]
    else:
        raise ImpactError("the new quantity needs a value, or a low and a high")
    return libyaml.set_quantity(old, library_path, changes)


def patched_text(tree: ProfilesTree, library_path: str, new: Mapping[str, Any]) -> tuple[str, str]:
    """``(old file text, new file text)`` for setting ``library_path`` to ``new``."""
    f = tree.library_file(library_path)
    if not f.exists():
        raise ImpactError(f"there is no library file for {library_path!r}")
    old = f.read_text()
    return old, patch_text(old, library_path, new)


def preview(
    store: Store,
    cfg: Settings,
    library_path: str,
    new_quantity: Mapping[str, Any],
    *,
    actor: str,
    arms: Iterable[str] = dv.ARM_PROFILES,
    tree: ProfilesTree | None = None,
) -> Impact:
    tree = tree or ProfilesTree(_profiles_root(cfg))
    old_text, new_text = patched_text(tree, library_path, new_quantity)
    rel = tree.library_file(library_path).relative_to(tree.root).as_posix()
    with tempfile.TemporaryDirectory(prefix="research-impact-") as tmp:
        new_lib = Path(tmp) / "library"
        shutil.copytree(tree.library, new_lib, ignore=shutil.ignore_patterns("schema", "review"))
        (new_lib / tree.library_file(library_path).name).write_text(new_text)
        before = dv.derive(tree, tree.library)
        after = dv.derive(tree, new_lib)
        rows, sums, unchanged = dv.diff(before, after)
        reflex = dv.reflex_rows(before, after, arms)
        params = wd.find_params_file(cfg)
        world_rows: list[WorldRow] = []
        reached = False
        if params is not None:
            world_rows, reached = wd.diff(
                params,
                tree.library_file(library_path),
                new_lib / tree.library_file(library_path).name,
            )
    sentences = [s.sentence for s in sums]
    if not sums:
        sentences.append("No profile value changes: no profile rule reads this value.")
    if params is None:
        note = "senseai-world is not installed here, so the world prior was not checked."
    elif reached:
        note = "The world prior changes: " + ", ".join(r.param for r in world_rows if r.changed)
    else:
        note = f"Unchanged: {WORLD_GAP}. PriorParams.from_library does not read {library_path}."
    sentences.append(note)
    for r in reflex:
        if r.theta_old != r.theta_new:
            sentences.append(
                f"{r.arm}: the reflex threshold θ moves from {r.theta_old:.2f} "
                f"to {r.theta_new:.2f}."
            )
    old_q = libyaml.get_quantity(old_text, library_path)
    imp = Impact(
        id=new_id("imp"),
        created_by=actor,
        library_path=library_path,
        library_file=rel,
        old_quantity={k: old_q.get(k) for k in ("value", "unit")},
        new_quantity=dict(new_quantity),
        library_patch=libyaml.unified_diff(old_text, new_text, rel),
        library_patch_sha=libyaml.sha(new_text),
        profiles_sha=git_sha(tree.root),
        consumers=consumers(tree, library_path),
        profile_rows=rows,
        field_summaries=sums,
        unchanged_fields=unchanged,
        reflex_rows=reflex,
        world_available=params is not None,
        world_rows=world_rows,
        world_reached=reached,
        world_note=note,
        sentences=sentences,
    )
    store.append("impact.recorded", actor, {"impact": imp.model_dump()})
    if params is not None and not reached:
        _log_world_gap(store, library_path, imp, actor)
    return imp


def _log_world_gap(store: Store, library_path: str, imp: Impact, actor: str) -> None:
    hid = f"hyp:world-gap:{library_path}"
    if store.get_raw("hypothesis", hid) is not None:
        return
    store.put_hypothesis(
        Hypothesis(
            id=hid,
            created_by=actor if not actor.startswith("agent:") else "pipeline:impact@0.1.0",
            statement=(
                f"{library_path} does not reach the world model yet: PriorParams.from_library "
                "does not read it, so the world prior keeps its default."
            ),
            origin=f"impact:{imp.id}",
            prediction=(
                "If world read this value, a correction to it would change the world prior's "
                "parameters as well as the profile."
            ),
            priority="high" if imp.consumers else "medium",
            target=library_path,
            consumers=imp.consumers,
        ),
        "pipeline:impact@0.1.0",
    )


def _profiles_root(cfg: Settings) -> Path:
    if cfg.profiles_repo is None:
        raise ImpactError("SENSEAI_PROFILES_REPO is not set (a profiles checkout is needed)")
    return cfg.profiles_repo


__all__ = ["WORLD_GAP", "ImpactError", "git_sha", "patch_text", "patched_text", "preview"]
