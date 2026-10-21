# SPDX-License-Identifier: Apache-2.0
"""Curate tests (moved from senseai-profiles): read the fixture profiles checkout."""

from __future__ import annotations

import importlib.util

import pytest

from tests.conftest import PROFILES

if importlib.util.find_spec("senseai_profiles") is None:
    # curate imports senseai_profiles at module level; without it (CI without the private
    # token) these modules cannot even be collected.
    collect_ignore_glob = ["test_*.py"]


@pytest.fixture(autouse=True)
def _profiles_root(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SENSEAI_PROFILES_REPO", str(PROFILES))
