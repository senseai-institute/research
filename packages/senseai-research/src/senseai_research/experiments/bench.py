# SPDX-License-Identifier: Apache-2.0
"""Run ``senseai-bench replay-compare`` as a subprocess and read what it wrote (spec §6).

The command is built by :func:`compare_command`, copied from the viewer
(``packages/senseai-viewer/server/senseai_viewer/runner/tools.py::compare_command`` at a787871),
because the viewer package is not importable here. Progress comes from the same
``events.jsonl`` the viewer tails.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shlex
import shutil
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from senseai_research.config import Settings

Progress = Callable[[dict[str, Any]], None]
#: DSEC sequence folder names (viewer runner/s3.py SEQ_RE).
SEQ_RE = re.compile(r"(zurich_city|interlaken|thun)_\d{2}_[a-z]")


class BenchError(RuntimeError):
    pass


def stack_tool(cfg: Settings, name: str) -> list[str]:
    """Command prefix for a stack CLI (viewer ``tools.stack_tool``, simplified)."""
    if name == "senseai-bench" and cfg.bench_cmd != "senseai-bench":
        return shlex.split(cfg.bench_cmd)
    venv_tool = cfg.stack / ".venv" / "bin" / name
    if venv_tool.exists():
        return [str(venv_tool)]
    if name == "senseai-bench":
        return shlex.split(cfg.bench_cmd)
    return [shutil.which(name) or name]


def compare_command(
    cfg: Settings,
    *,
    sequences: list[Path],
    arms: list[str],
    out_dir: Path,
    events: Path,
    runlog_dir: Path,
    run_id: str,
    max_seconds: float | None = None,
    profiles_repo: Path | None = None,
) -> list[str]:
    """The exact ``senseai-bench replay-compare`` command (copied from the viewer runner)."""
    cmd = [*stack_tool(cfg, "senseai-bench"), "replay-compare"]
    for s in sequences:
        cmd += ["--sequence", str(s)]
    cmd += ["--arms", ",".join(arms)]
    if max_seconds:
        cmd += ["--max-seconds", f"{max_seconds:g}"]
    if profiles_repo is not None:
        cmd += ["--profiles-repo", str(profiles_repo)]
    cmd += [
        "--out",
        str(out_dir),
        "--events",
        str(events),
        "--runlog",
        f"{runlog_dir}/",
        "--run-id",
        run_id,
    ]
    return cmd


def find_sequences(root: Path | None, names: list[str], max_depth: int = 3) -> list[Path]:
    """Sequence folders by name under ``SENSEAI_DATA_ROOT`` (viewer ``prepare.find_local``)."""
    found: dict[str, Path] = {}

    def walk(d: Path, depth: int) -> None:
        if (d / "images" / "left").is_dir() or (SEQ_RE.fullmatch(d.name) and depth > 0):
            found.setdefault(d.name, d)
            return
        if depth >= max_depth:
            return
        with contextlib.suppress(OSError):
            for c in sorted(d.iterdir()):
                if c.is_dir() and not c.name.startswith("."):
                    walk(c, depth + 1)

    out: list[Path] = []
    for n in names:
        p = Path(n).expanduser()
        if p.is_dir():
            out.append(p)
            continue
        if root is None:
            raise BenchError(f"recording {n} not found: SENSEAI_DATA_ROOT is not set")
        if not found:
            walk(root, 0)
        if n not in found:
            have = ", ".join(sorted(found)) or "none"
            raise BenchError(f"recording {n} is not on this machine (have: {have})")
        out.append(found[n])
    return out


def read_events(path: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    with contextlib.suppress(OSError):
        for line in path.read_text(encoding="utf-8").splitlines():
            with contextlib.suppress(ValueError):
                obj = json.loads(line)
                if isinstance(obj, dict):
                    out.append(obj)
    return out


@dataclass
class _Tail:
    path: Path
    on_event: Progress
    pos: int = 0
    buf: bytes = b""
    stop: threading.Event = field(default_factory=threading.Event)

    def poll(self) -> None:
        try:
            with self.path.open("rb") as f:
                f.seek(self.pos)
                chunk = f.read()
        except OSError:
            return
        self.pos += len(chunk)
        self.buf += chunk
        *lines, self.buf = self.buf.split(b"\n")
        for raw in lines:
            with contextlib.suppress(ValueError):
                ev = json.loads(raw)
                if isinstance(ev, dict):
                    self.on_event(ev)

    def run(self) -> None:
        while not self.stop.wait(0.5):
            self.poll()
        self.poll()


@dataclass
class BenchRun:
    command: list[str]
    returncode: int
    report: dict[str, Any] | None
    report_dir: Path
    events_file: Path
    log: str


def run(
    cmd: list[str],
    *,
    run_dir: Path,
    events: Path,
    report_dir: Path,
    env: dict[str, str] | None = None,
    on_event: Progress | None = None,
    timeout: float | None = None,
) -> BenchRun:
    """Run bench with ``cwd=run_dir`` (as the viewer does), tailing ``events`` while it runs."""
    run_dir.mkdir(parents=True, exist_ok=True)
    tail = _Tail(events, on_event or (lambda _e: None))
    th = threading.Thread(target=tail.run, daemon=True)
    th.start()
    log_path = run_dir / "bench.log"
    t0 = time.monotonic()
    try:
        with log_path.open("w") as log:
            proc = subprocess.run(
                cmd,
                cwd=run_dir,
                env={**os.environ, **(env or {})},
                stdout=log,
                stderr=subprocess.STDOUT,
                timeout=timeout,
                check=False,
            )
    except FileNotFoundError as e:
        raise BenchError(f"cannot run {cmd[0]!r}: is senseai-bench installed? ({e})") from e
    finally:
        tail.stop.set()
        th.join(5)
    rj = report_dir / "replay_report.json"
    report = json.loads(rj.read_text()) if rj.exists() else None
    log_text = log_path.read_text(errors="replace")
    if report is None and proc.returncode != 0:
        raise BenchError(
            f"senseai-bench failed after {time.monotonic() - t0:.0f} s: {log_text[-800:].strip()}"
        )
    return BenchRun(cmd, proc.returncode, report, report_dir, events, log_text)


__all__ = [
    "SEQ_RE",
    "BenchError",
    "BenchRun",
    "compare_command",
    "find_sequences",
    "read_events",
    "run",
    "stack_tool",
]
