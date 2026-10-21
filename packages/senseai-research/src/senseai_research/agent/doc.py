# SPDX-License-Identifier: Apache-2.0
"""Generate ``docs/AGENT.md`` from the code: tools with their kinds, the model call, the system
prompt and the guardrails. ``senseai-research agent-doc`` writes it; a test keeps it current."""

from __future__ import annotations

import json

from senseai_research.agent.loop import BETAS, EFFORT, MAX_TOKENS, THINKING
from senseai_research.agent.prompts import SYSTEM_PROMPT
from senseai_research.agent.tools import TOOLS
from senseai_research.config import DEFAULT_MODEL

KIND_TEXT = {
    "read": "read: runs freely, changes nothing",
    "compute": "compute: runs freely within a rate limit; never changes a claim or the library",
    "write": (
        "write-pending: creates an Approve/Reject card; nothing changes until a person approves"
    ),
}

GUARDRAILS = (
    (
        "N1",
        "Write tools only create pending actions (`actions.py`); the loop ends the turn after "
        "one. `actions.approve` records the Approval (person, rationale) before applying. The "
        "ledger refuses claim changes by `agent:` actors and verified/rejected/superseded without "
        "an Approval (`ledger/store.py`). Only `proposals/github.py` pushes, and only with an "
        "Approval row.",
    ),
    (
        "N2",
        "Quotes are located in stored fetched text by code (`literature/passages.py`) before a "
        "card is shown; values must appear in their quote (curate `_value_in_quote`); passages "
        "must equal the stored text at their offsets; unverified claims cannot be proposed.",
    ),
    (
        "N3",
        "SQLite triggers refuse UPDATE/DELETE on events, fetched texts and transcripts; claims "
        "change only by revision events with a reason.",
    ),
    (
        "N4",
        "`impact_preview` runs `consumers_by_path`, re-derives every profile and runs "
        "`PriorParams.from_library`; the record is stored and quoted.",
    ),
    (
        "N5",
        "Only `experiment_get` returns performance numbers, from stored records. After each "
        "turn `agent/numbers.py` checks every performance number in the reply against those "
        "results and flags the rest to the person.",
    ),
)


def render() -> str:
    lines = [
        "# The research agent",
        "",
        "Generated from the code by `senseai-research agent-doc`. Do not edit by hand.",
        "",
        "## Model call",
        "",
        "| setting | value |",
        "|---|---|",
        f"| model | `{DEFAULT_MODEL}` (override with `RESEARCH_MODEL`) |",
        "| API | `client.beta.messages.stream`, manual loop (`agent/loop.py`) |",
        f"| thinking | `{json.dumps(THINKING)}` |",
        f"| effort | `{EFFORT}` |",
        f"| max_tokens | {MAX_TOKENS} |",
        f'| betas | `{", ".join(BETAS)}` with `fallbacks: "default"` |',
        "| caching | top-level `cache_control: {type: ephemeral}`; "
        "frozen system prompt, sorted tools |",
        "| tools | `strict: true`, `eager_input_streaming: true`; "
        "inputs validated before running |",
        "| stop reasons | `refusal` and `max_tokens` end the turn without running tools; "
        "`pause_turn` is resumed by re-sending |",
        "",
        "## Tools",
        "",
        "| tool | kind | what it does |",
        "|---|---|---|",
    ]
    for t in sorted(TOOLS, key=lambda t: (["read", "compute", "write"].index(t.kind), t.name)):
        kind = "write-pending" if t.kind == "write" else t.kind
        lines.append(f"| `{t.name}` | {kind} | {t.description} |")
    lines += ["", "Kinds:", ""]
    lines += [f"- **{k}**: {v.split(': ', 1)[1]}" for k, v in KIND_TEXT.items()]
    lines += [
        "",
        "There is no tool that edits files, runs shell commands or calls GitHub.",
        "",
        "## Guardrails",
        "",
    ]
    lines += [f"- **{n}.** {text}" for n, text in GUARDRAILS]
    lines += ["", "## System prompt", "", "```text", SYSTEM_PROMPT.rstrip(), "```", ""]
    return "\n".join(lines)


__all__ = ["render"]
