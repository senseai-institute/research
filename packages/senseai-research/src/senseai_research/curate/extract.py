# SPDX-License-Identifier: Apache-2.0
"""Extract claims from paper text with the Anthropic API, enforcing verbatim quotes in code.

One call per (paper, clade). The model may only report numbers that appear
verbatim in the text we sent, with the sentence as ``quote``. The checks that
matter run here, not in the prompt:

1. ``quote`` is a substring of the normalised text, else ``quote_not_found``;
2. the number appears inside the quote, else ``value_not_in_quote``;
3. ``path`` is in the catalogue, else ``unknown_path``;
4. the unit normalises, else the claim is kept with ``unit_unparsed`` flagged.

Raw tool outputs are cached before any check (``ModelCache``) so a rejected
claim is auditable and a re-run costs nothing.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from senseai_research.curate import CURATOR
from senseai_research.curate.cache import ModelCache
from senseai_research.curate.catalogue import Catalogue
from senseai_research.curate.fetch import PaperText, normalise
from senseai_research.curate.units import normalise_unit

DEFAULT_MODEL = "claude-opus-5-5"

SYSTEM_PROMPT = (
    "You are extracting measured sensory values from one scientific paper for a species "
    "library. Report only numbers that appear verbatim in the text, with the exact sentence "
    "that contains them as `quote` (copy it; do not edit, shorten or paraphrase). Never "
    "compute, convert, round, or infer a value. If the paper reports a value for a species "
    "outside the clade, record it with `species` set. If it reports a range, use "
    "`value_low`/`value_high` and set `value` to null. If the paper is a review quoting "
    "another paper, set `source_type` to `review` and put the cited reference in `cites`. "
    "If nothing in the catalogue is measured here, call record_claims with an empty `claims` "
    "list. Always answer by calling the record_claims tool."
)

RECORD_CLAIMS_TOOL: dict[str, Any] = {
    "name": "record_claims",
    "description": (
        "Record measured sensory values found verbatim in the paper, mapped to library "
        "schema paths."
    ),
    "strict": True,
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["doi", "claims", "species_studied", "paper_type"],
        "properties": {
            "doi": {"type": "string"},
            "species_studied": {"type": "array", "items": {"type": "string"}},
            "paper_type": {"type": "string", "enum": ["primary", "review", "dataset", "other"]},
            "claims": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "path",
                        "value",
                        "value_low",
                        "value_high",
                        "unit",
                        "quote",
                        "section",
                        "conditions",
                        "species",
                        "source_type",
                        "cites",
                        "confidence_in_mapping",
                    ],
                    "properties": {
                        "path": {"type": "string"},
                        "value": {"type": ["number", "null"]},
                        "value_low": {"type": ["number", "null"]},
                        "value_high": {"type": ["number", "null"]},
                        "unit": {"type": "string"},
                        "quote": {"type": "string", "maxLength": 300},
                        "section": {"type": "string"},
                        "conditions": {"type": "string"},
                        "species": {"type": ["string", "null"]},
                        "source_type": {
                            "type": "string",
                            "enum": ["primary", "review", "abstract_only"],
                        },
                        "cites": {"type": ["string", "null"]},
                        "confidence_in_mapping": {
                            "type": "string",
                            "enum": ["exact", "close", "loose"],
                        },
                    },
                },
            },
        },
    },
}


@dataclass
class Claim:
    doi: str
    path: str
    value: float | None
    value_low: float | None
    value_high: float | None
    unit: str
    quote: str
    section: str
    conditions: str
    species: str | None
    source_type: str  # primary | review | abstract_only
    cites: str | None
    mapping: str  # exact | close | loose
    fulltext: bool
    flags: list[str] = field(default_factory=list)
    canonical_unit: str | None = None
    canonical_value: float | None = None
    canonical_low: float | None = None
    canonical_high: float | None = None

    @property
    def is_range(self) -> bool:
        return self.value is None and self.value_low is not None and self.value_high is not None

    def to_dict(self) -> dict[str, Any]:
        return {k: getattr(self, k) for k in self.__dataclass_fields__}


@dataclass
class Extraction:
    doi: str
    claims: list[Claim]
    rejected: list[tuple[dict[str, Any], str]]  # (raw claim, reason)
    paper_type: str
    species_studied: list[str]
    status: str  # ok | no_claims | refusal | no_tool_call | empty_text | cached
    raw: dict[str, Any] = field(default_factory=dict)


# --- the checks -----------------------------------------------------------------

# A sign counts only when it does not follow a digit ("26,000-27,000" is two numbers).
_NUM = re.compile(r"(?<![\w.])[-+]?\d[\d,]*\.?\d*(?:[eE][-+]?\d+)?")


def _numbers_in(text: str) -> set[float]:
    out: set[float] = set()
    for m in _NUM.finditer(text):
        try:
            out.add(float(m.group(0).replace(",", "").rstrip(".")))
        except ValueError:
            continue
    return out


def _value_in_quote(value: float, quote: str) -> bool:
    nums = _numbers_in(quote)
    return any(abs(n - value) <= 1e-9 * max(1.0, abs(value)) for n in nums)


def check_claim(
    raw: Mapping[str, Any], text: str, catalogue: Catalogue
) -> tuple[Claim | None, str]:
    """Apply the hard checks to one raw claim. Returns ``(claim, "")`` or ``(None, reason)``."""
    quote = normalise(str(raw.get("quote", "")))
    if not quote or quote not in text:
        return None, "quote_not_found"
    value, lo, hi = raw.get("value"), raw.get("value_low"), raw.get("value_high")
    if value is None and (lo is None or hi is None):
        return None, "no_value"
    for v in (x for x in (value, lo, hi) if x is not None):
        if not _value_in_quote(float(v), quote):
            return None, "value_not_in_quote"
    path = str(raw.get("path", ""))
    if path not in catalogue.by_path():
        return None, "unknown_path"
    c = Claim(
        doi=str(raw.get("doi", "")),
        path=path,
        value=float(value) if value is not None else None,
        value_low=float(lo) if lo is not None else None,
        value_high=float(hi) if hi is not None else None,
        unit=str(raw.get("unit", "")),
        quote=quote,
        section=str(raw.get("section", "")),
        conditions=str(raw.get("conditions", "")),
        species=raw.get("species"),
        source_type=str(raw.get("source_type", "primary")),
        cites=raw.get("cites"),
        mapping=str(raw.get("confidence_in_mapping", "loose")),
        fulltext=bool(raw.get("_fulltext", False)),
    )
    norm = normalise_unit(c.unit)
    if norm is None:
        c.flags.append("unit_unparsed")
    else:
        canon, factor = norm
        c.canonical_unit = canon
        if c.value is not None:
            c.canonical_value = c.value * factor
        if c.value_low is not None:
            c.canonical_low = c.value_low * factor
        if c.value_high is not None:
            c.canonical_high = c.value_high * factor
        spec = catalogue.by_path()[path]
        if canon not in spec.units and c.unit not in spec.units:
            c.flags.append("unit_not_in_catalogue")
    if c.mapping == "loose":
        c.flags.append("loose_mapping")
    if not c.fulltext and c.source_type == "primary":
        c.source_type = "abstract_only"
    return c, ""


def verify(tool_input: Mapping[str, Any], paper: PaperText, catalogue: Catalogue) -> Extraction:
    """Turn a raw ``record_claims`` input into checked claims."""
    text = normalise(paper.text)
    claims: list[Claim] = []
    rejected: list[tuple[dict[str, Any], str]] = []
    for item in tool_input.get("claims", []):
        raw = {**item, "doi": paper.doi, "_fulltext": paper.fulltext}
        c, reason = check_claim(raw, text, catalogue)
        if c is None:
            rejected.append((dict(raw), reason))
        else:
            claims.append(c)
    return Extraction(
        doi=paper.doi,
        claims=claims,
        rejected=rejected,
        paper_type=str(tool_input.get("paper_type", "other")),
        species_studied=[str(s) for s in tool_input.get("species_studied", [])],
        status="ok" if claims else "no_claims",
        raw=dict(tool_input),
    )


# --- the model call ---------------------------------------------------------------

ModelCall = Callable[[dict[str, Any]], dict[str, Any]]
"""Takes the request payload, returns ``{"stop_reason": ..., "tool_input": {...} | None}``."""


def build_request(paper: PaperText, catalogue: Catalogue, *, model: str) -> dict[str, Any]:
    return {
        "model": model,
        "max_tokens": 16000,
        "output_config": {"effort": "medium"},
        "system": [
            {"type": "text", "text": SYSTEM_PROMPT},
            {
                "type": "text",
                "text": catalogue.prompt_text(),
                "cache_control": {"type": "ephemeral"},
            },
        ],
        "tools": [RECORD_CLAIMS_TOOL],
        "tool_choice": {"type": "auto"},
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "document",
                        "source": {
                            "type": "text",
                            "media_type": "text/plain",
                            "data": normalise(paper.text),
                        },
                        "title": paper.doi,
                        "citations": {"enabled": False},
                    },
                    {
                        "type": "text",
                        "text": (
                            f"The paper's DOI is {paper.doi}. Record every measured value in it "
                            "that fills one of the catalogue paths, using record_claims."
                        ),
                    },
                ],
            }
        ],
    }


def anthropic_call(payload: dict[str, Any]) -> dict[str, Any]:
    """The real call, through research's one client factory (``agent.client.make_client``)."""
    from senseai_research.agent.client import make_client

    client = make_client()
    resp = client.beta.messages.create(
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        **payload,
    )
    tool_input: dict[str, Any] | None = None
    for block in resp.content:
        if block.type == "tool_use" and block.name == "record_claims":
            tool_input = dict(block.input)
            break
    return {
        "stop_reason": resp.stop_reason,
        "tool_input": tool_input,
        "usage": {
            "input": resp.usage.input_tokens,
            "output": resp.usage.output_tokens,
            "cache_read": getattr(resp.usage, "cache_read_input_tokens", 0),
        },
        "model": resp.model,
    }


def extract(
    paper: PaperText,
    catalogue: Catalogue,
    *,
    cache: ModelCache,
    call: ModelCall | None = None,
    model: str | None = None,
) -> Extraction:
    """Extract and verify claims for one paper, through the model cache."""
    if not paper.text.strip():
        return Extraction(paper.doi, [], [], "other", [], "empty_text")
    model = model or os.environ.get("SENSEAI_CURATE_MODEL", DEFAULT_MODEL)
    payload = build_request(paper, catalogue, model=model)
    key = ModelCache.key(
        {
            "model": model,
            "system": payload["system"],
            "tool": RECORD_CLAIMS_TOOL,
            "doc": payload["messages"][0]["content"][0]["source"]["data"],
        }
    )
    result = cache.get(key)
    cached = result is not None
    if result is None:
        result = (call or anthropic_call)(payload)
        result["curator"] = CURATOR
        cache.put(key, result)
    if result.get("stop_reason") == "refusal":
        return Extraction(paper.doi, [], [], "other", [], "refusal", raw=result)
    if not result.get("tool_input"):
        return Extraction(paper.doi, [], [], "other", [], "no_tool_call", raw=result)
    ex = verify(result["tool_input"], paper, catalogue)
    if cached and ex.status == "ok":
        ex.status = "cached"
    ex.raw = result
    return ex


__all__ = [
    "DEFAULT_MODEL",
    "RECORD_CLAIMS_TOOL",
    "SYSTEM_PROMPT",
    "Claim",
    "Extraction",
    "ModelCall",
    "anthropic_call",
    "build_request",
    "check_claim",
    "extract",
    "verify",
]
