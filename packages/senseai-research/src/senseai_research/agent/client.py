# SPDX-License-Identifier: Apache-2.0
"""The one place an Anthropic client is made (spec §13).

Tests replace the factory with one whose HTTP transport replays recorded responses, so no
test needs a key or the network. ``ANTHROPIC_API_KEY`` (or any credential the SDK resolves,
such as an ``ant auth`` profile) is read by the SDK itself.
"""

from __future__ import annotations

from collections.abc import Callable

import anthropic

Factory = Callable[[], anthropic.Anthropic]
_factory: Factory | None = None


def set_client_factory(factory: Factory | None) -> None:
    global _factory
    _factory = factory


def make_client() -> anthropic.Anthropic:
    if _factory is not None:
        return _factory()
    return anthropic.Anthropic(max_retries=2)


__all__ = ["Factory", "make_client", "set_client_factory"]
