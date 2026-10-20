# SPDX-License-Identifier: Apache-2.0
"""A temporary copy of the profiles checkout with a patched library file (spec §6).

The base checkout (``SENSEAI_PROFILES_REPO``) is only read: a git checkout is cloned with
``--shared`` (which writes nothing to the source) at its current ``HEAD``; a plain directory is
copied. The patch is applied in the copy, and every profile the chosen arms use is re-distilled
into the copy's ``priors/``, because bench packages bundles, not library YAML (Deviations 4).
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from senseai_research.impact import git_sha
from senseai_research.impact.consumers import ProfilesTree


class WorkspaceError(RuntimeError):
    pass


@dataclass
class Workspace:
    root: Path
    sha: str
    patched_file: str | None
    distill_log: str

    @property
    def tree(self) -> ProfilesTree:
        return ProfilesTree(self.root)

    def cleanup(self) -> None:
        shutil.rmtree(self.root.parent, ignore_errors=True)


def _copy(base: Path, dest: Path) -> str:
    if (base / ".git").exists():
        sha = git_sha(base)
        for cmd in (
            ["git", "clone", "--quiet", "--shared", "--no-checkout", str(base), str(dest)],
            ["git", "-C", str(dest), "checkout", "--quiet", "--detach", sha],
        ):
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
            if r.returncode != 0:
                raise WorkspaceError(f"{' '.join(cmd[:3])} failed: {r.stderr.strip()}")
        return sha
    shutil.copytree(base, dest, ignore=shutil.ignore_patterns(".git", ".venv", "node_modules"))
    return git_sha(base)


def distill_command(python: str, root: Path, names: Iterable[str]) -> list[str]:
    code = "import sys; from senseai_profiles.cli import main; sys.exit(main(sys.argv[1:]))"
    return [
        python,
        "-c",
        code,
        "--root",
        str(root),
        "--sensors",
        str(root / "sensors"),
        "distill",
        *names,
        "--out",
        str(ProfilesTree(root).priors),
    ]


def make_workspace(
    base: Path,
    *,
    profiles: Iterable[str],
    library_file: str | None = None,
    new_text: str | None = None,
    python: str | None = None,
    parent: Path | None = None,
) -> Workspace:
    """Copy ``base``, write ``new_text`` to ``library_file`` (repo-relative), re-distill."""
    tmp = Path(tempfile.mkdtemp(prefix="research-ws-", dir=parent))
    root = tmp / "profiles"
    sha = _copy(base, root)
    if library_file is not None:
        if new_text is None:
            raise WorkspaceError("a library file to patch needs its new text")
        target = root / library_file
        if not target.is_file():
            raise WorkspaceError(f"{library_file} is not in the profiles checkout")
        target.write_text(new_text)
    names = sorted(set(profiles))
    log = ""
    if names:
        cmd = distill_command(python or sys.executable, root, names)
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        log = (r.stdout + r.stderr).strip()
        if r.returncode != 0:
            shutil.rmtree(tmp, ignore_errors=True)
            raise WorkspaceError(f"re-distilling {', '.join(names)} failed: {log[-800:]}")
    return Workspace(root=root, sha=sha, patched_file=library_file, distill_log=log)


__all__ = ["Workspace", "WorkspaceError", "distill_command", "make_workspace"]
