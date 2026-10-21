# SPDX-License-Identifier: Apache-2.0
"""From evidence to circuit inputs: quality per claim, estimates per knob, the package that
research hands to senseai-circuits, and its approval-gated release (docs/HANDOFF.md).

Research's work ends here: circuits builds the model from the package.
"""

from __future__ import annotations

from senseai_research.handoff.package import (
    FORMAT,
    Handoff,
    HandoffError,
    Policy,
    build,
    clades,
    claim_quality,
    schema,
    verify,
)

__all__ = [
    "FORMAT",
    "Handoff",
    "HandoffError",
    "Policy",
    "build",
    "clades",
    "claim_quality",
    "schema",
    "verify",
]
