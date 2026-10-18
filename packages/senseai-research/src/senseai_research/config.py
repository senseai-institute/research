# SPDX-License-Identifier: Apache-2.0
"""Settings from the environment (spec §9).

Variable names follow the viewer runner (``SENSEAI_DATA_ROOT``, ``SENSEAI_BENCH_CMD``,
``SENSEAI_BENCH_PYTHON``) so ``viewer.env`` can be sourced as-is.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, replace
from pathlib import Path

DEFAULT_MODEL = "claude-opus-5-5"


def _path(name: str) -> Path | None:
    v = os.environ.get(name)
    return Path(v).expanduser() if v else None


@dataclass(frozen=True)
class Settings:
    data: Path
    port: int = 8100
    profiles_repo: Path | None = None
    data_root: Path | None = None
    bench_cmd: str = "senseai-bench"
    bench_python: str | None = None
    github_token: str | None = None
    github_repo: str = "senseai-institute/profiles"
    github_api: str = "https://api.github.com"
    github_remote: str | None = None  # git push URL; default https://github.com/<repo>.git
    github_base: str = "main"
    curate_s3: str | None = None
    model: str = DEFAULT_MODEL
    stack_dir: Path | None = None
    world_params: Path | None = None

    @property
    def db_path(self) -> Path:
        return self.data / "ledger.sqlite"

    @property
    def runs_dir(self) -> Path:
        return self.data / "runs"

    @property
    def cache_dir(self) -> Path:
        return self.data / "cache"

    @property
    def outbox(self) -> Path:
        """Patch files and PR bodies written when there is no ``GITHUB_TOKEN``."""
        return self.data / "outbox"

    @property
    def stack(self) -> Path:
        """Folder holding the SenseAI checkouts side by side (the viewer's convention)."""
        return self.stack_dir or Path.home() / "senseai"

    def with_(self, **kw: object) -> Settings:
        return replace(self, **kw)  # type: ignore[arg-type]

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            data=_path("RESEARCH_DATA") or Path.home() / "senseai" / "research",
            port=int(os.environ.get("RESEARCH_PORT", "8100")),
            profiles_repo=_path("SENSEAI_PROFILES_REPO"),
            data_root=_path("SENSEAI_DATA_ROOT"),
            bench_cmd=os.environ.get("SENSEAI_BENCH_CMD", "senseai-bench"),
            bench_python=os.environ.get("SENSEAI_BENCH_PYTHON") or None,
            github_token=os.environ.get("GITHUB_TOKEN") or None,
            github_repo=os.environ.get("RESEARCH_GITHUB_REPO", "senseai-institute/profiles"),
            github_api=os.environ.get("RESEARCH_GITHUB_API", "https://api.github.com"),
            curate_s3=os.environ.get("RESEARCH_CURATE_S3") or None,
            github_remote=os.environ.get("RESEARCH_GITHUB_REMOTE") or None,
            github_base=os.environ.get("RESEARCH_GITHUB_BASE", "main"),
            model=os.environ.get("RESEARCH_MODEL", DEFAULT_MODEL),
            stack_dir=_path("SENSEAI_STACK_DIR"),
            world_params=_path("SENSEAI_WORLD_PARAMS"),
        )


__all__ = ["DEFAULT_MODEL", "Settings"]
