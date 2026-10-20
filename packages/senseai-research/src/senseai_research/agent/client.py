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


NO_KEY = (
    "The assistant has no Claude API key: set ANTHROPIC_API_KEY on the research server "
    "(the Settings tab lists every key)."
)


def missing_credentials(e: BaseException) -> bool:
    """The SDK found no API key or other credential (it raises ``TypeError`` at request time)."""
    return isinstance(e, TypeError) and "authentication method" in str(e)


__all__ = ["NO_KEY", "Factory", "make_client", "missing_credentials", "set_client_factory"]
