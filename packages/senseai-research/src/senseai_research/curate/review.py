# SPDX-License-Identifier: Apache-2.0
"""Review sheet (``library/review/<clade>.md``) and the ``approve`` gate.

The sheet is generated from the draft entry plus the run state; a human fills
the **Reviewer decision** column with ``accept``, ``accept value=<v>``,
``reject`` or ``defer`` (optionally followed by a note). ``approve`` parses the
column back, refuses while any ⚠ row (confidence low, conflict or flag) lacks
a decision, writes ``reviewer_note`` and flips ``review_status`` to
``reviewed``.
"""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from senseai_profiles.library import validate_entry

from senseai_research.curate import CURATOR
from senseai_research.curate.assemble import dump_entry

DECISIONS = ("accept", "reject", "defer")
_DECISION = re.compile(r"^(accept|reject|defer)(?:\s+value=(\S+))?\s*(.*)$", re.IGNORECASE)
_ROW = re.compile(r"^\|\s*(\d+)\s*\|\s*([^|]+?)\s*\|(.*)\|\s*([^|]*?)\s*\|$")
_SECTION = re.compile(r"^## (senses\.[\w-]+|behaviours\.[\w-]+)$")
_WARN = "⚠"


@dataclass
class Row:
    n: int
    path: str
    value: str
    unit: str
    confidence: str
    papers: int
    agreement: str
    flags: list[str]
    evidence: str
    needs_decision: bool
    section: str


@dataclass
class Decision:
    path: str
    verdict: str  # accept | reject | defer
    value: str | None
    note: str

    @property
    def text(self) -> str:
        v = f"{self.verdict}" + (f" value={self.value}" if self.value else "")
        return (v + (f": {self.note}" if self.note else "")).strip()


@dataclass
class SheetInput:
    """Everything the sheet needs beyond the entry, from the run state."""

    clade_key: str
    curated_at: str
    cache_digest: str
    papers_screened: int
    per_source: Mapping[str, int]
    fulltext: int
    abstract_only: int
    claims_extracted: int
    counts: Mapping[str, int]  # high/medium/low
    flags_detail: Mapping[str, list[str]] = field(default_factory=dict)  # kind → lines
    paper_flags: Mapping[str, list[str]] = field(default_factory=dict)  # doi → flags
    agreement: Mapping[str, str] = field(default_factory=dict)  # path → text
    empty_paths: Mapping[str, str] = field(default_factory=dict)  # path → why / queries tried
    queries: Iterable[str] = ()
    placeholders: Iterable[str] = ()


def _fmt_value(v: Any) -> str:
    if isinstance(v, list):
        return f"{v[0]:g}–{v[1]:g}"
    return f"{v:g}" if isinstance(v, int | float) else str(v)


def _cell(s: str) -> str:
    return s.replace("|", "\\|").replace("\n", " ")


def _quantities(entry: Mapping[str, Any]) -> list[tuple[str, str, dict[str, Any]]]:
    """``(section, path, quantity)`` for every curated quantity in the entry, in entry order."""
    out: list[tuple[str, str, dict[str, Any]]] = []
    for s in entry.get("senses", []):
        sec = f"senses.{s['id']}"
        for k, v in s.items():
            if isinstance(v, dict) and "value" in v:
                out.append((sec, f"{sec}.{k}", v))
        for m in s.get("measurements", []):
            out.append((sec, f"{sec}.measurements.{m['id']}", m))
    for b in entry.get("behaviours", []):
        sec = f"behaviours.{b['name']}"
        for k, v in b.items():
            if isinstance(v, dict) and "value" in v:
                out.append((sec, f"{sec}.{k}", v))
    return out


def _evidence_cell(q: Mapping[str, Any], primary: str | None) -> str:
    parts: list[str] = []
    for e in q.get("evidence", []):
        doi = e["doi"]
        label = f"**{doi}**" if doi == primary else doi
        kind = e.get("source_type", "?") + (", full text" if e.get("fulltext") else ", abstract")
        where = e.get("page_or_section", "")
        parts.append(f'{label} ({kind}) {where}: "{_cell(e["quote"])}"')
    if not parts:
        parts.append("hand-written; no verbatim evidence on file")
    return " · ".join(parts)


def rows_for(entry: Mapping[str, Any], info: SheetInput) -> list[Row]:
    rows: list[Row] = []
    for i, (sec, path, q) in enumerate(_quantities(entry), 1):
        dois = [e["doi"] for e in q.get("evidence", [])] or list(q.get("citations", []))
        flags = sorted({f for d in dois for f in info.paper_flags.get(d, [])})
        agreement = info.agreement.get(path, "single" if len(set(dois)) <= 1 else "agree")
        primary = dois[0] if dois else None
        conf = str(q.get("confidence", "?"))
        warn = conf == "low" or bool(flags) or agreement.startswith("conflict")
        rows.append(
            Row(
                i,
                path,
                _fmt_value(q.get("value")),
                str(q.get("unit", "")),
                conf,
                len(set(dois)),
                agreement,
                flags,
                _evidence_cell(q, primary),
                warn and not q.get("reviewer_note"),
                sec,
            )
        )
    return rows


def render_sheet(entry: Mapping[str, Any], info: SheetInput) -> str:
    rows = rows_for(entry, info)
    counts = info.counts
    src = ", ".join(f"{k} {v}" for k, v in info.per_source.items())
    lines = [
        f"# Review sheet: {entry['clade']} ({info.clade_key}.yaml)",
        "",
        f"- draft: {entry.get('curated_by', CURATOR)}, {info.curated_at}, cache digest "
        f"`{info.cache_digest[:12]}`",
        f"- papers screened: {info.papers_screened} ({src})",
        f"- full text available: {info.fulltext} · abstract only: {info.abstract_only} · "
        "paywalled (skipped): 0",
        f"- claims extracted: {info.claims_extracted} → after reconciliation "
        f"{sum(counts.values())} values (high {counts.get('high', 0)} · "
        f"medium {counts.get('medium', 0)} · low {counts.get('low', 0)})",
        "- flags: "
        + (", ".join(f"{len(v)} {k}" for k, v in info.flags_detail.items() if v) or "none"),
        f"- schema paths still empty: {len(info.empty_paths)} (listed at the end)",
        "",
        "Fill **Reviewer decision** with one of: `accept`, `accept value=<v>` (pick one of the",
        "listed values), `reject`, `defer`, optionally followed by a note.",
        "`approve` requires a decision on every row marked ⚠ (confidence low or any flag).",
        "",
    ]
    header = (
        "| # | path | value | unit | conf | papers | agreement | flags | evidence "
        "| Reviewer decision |"
    )
    sep = "|---|------|-------|------|------|--------|-----------|-------|----------|---|"
    current = None
    for r in rows:
        if r.section != current:
            current = r.section
            lines += [f"## {r.section}", "", header, sep]
        short = r.path[len(r.section) + 1 :]
        warn = f" {_WARN}" if r.needs_decision else ""
        lines.append(
            f"| {r.n} | {short} | {r.value}{warn} | {r.unit} | {r.confidence} | {r.papers} "
            f"| {_cell(r.agreement)} | {_cell(', '.join(r.flags)) or '—'} | {r.evidence} |  |"
        )
        if r.section != next((x.section for x in rows if x.n == r.n + 1), None):
            lines.append("")
    if info.placeholders:
        lines += ["## Placeholders to fill by hand", ""]
        lines += [f"- {p}: organ/signal/description are TODO" for p in info.placeholders]
        lines.append("")
    lines += ["## Flags in detail", ""]
    any_flag = False
    for kind, items in info.flags_detail.items():
        for it in items:
            any_flag = True
            lines.append(f"- **{kind}**: {it}")
    if not any_flag:
        lines.append("- none")
    lines += ["", "## Schema paths still empty", ""]
    lines += [f"- {p} — {why}" for p, why in info.empty_paths.items()] or ["- none"]
    qs = list(info.queries)
    lines += ["", f"## Queries tried ({len(qs)})", "", "<details>", ""]
    lines += [f"- {q}" for q in qs]
    lines += ["", "</details>", ""]
    return "\n".join(lines)


def parse_decisions(text: str) -> dict[str, Decision]:
    out: dict[str, Decision] = {}
    section = ""
    for line in text.splitlines():
        m = _SECTION.match(line.strip())
        if m:
            section = m.group(1)
            continue
        r = _ROW.match(line.strip())
        if not r:
            continue
        short, decision = r.group(2), r.group(4).strip()
        if short == "path" or not decision:
            continue
        d = _DECISION.match(decision)
        path = f"{section}.{short}" if section else short
        if not d:
            raise ValueError(f"row {r.group(1)} ({path}): cannot parse decision {decision!r}")
        note = d.group(3).strip().lstrip(":;-–— ").strip()
        out[path] = Decision(path, d.group(1).lower(), d.group(2), note)
    return out


def warn_rows(text: str) -> set[str]:
    """Paths of ⚠ rows in a sheet."""
    out: set[str] = set()
    section = ""
    for line in text.splitlines():
        m = _SECTION.match(line.strip())
        if m:
            section = m.group(1)
            continue
        r = _ROW.match(line.strip())
        if r and _WARN in r.group(3).split("|")[0]:
            out.add(f"{section}.{r.group(2)}")
    return out


def _find(entry: dict[str, Any], path: str) -> tuple[Any, str | int]:
    """Container and key holding the quantity at ``path``."""
    parts = path.split(".")
    if parts[0] == "senses":
        sense = next(s for s in entry["senses"] if s["id"] == parts[1])
        if len(parts) == 4:
            items = sense["measurements"]
            return items, next(i for i, m in enumerate(items) if m["id"] == parts[3])
        return sense, parts[2]
    b = next(b for b in entry["behaviours"] if b["name"] == parts[1])
    return b, parts[2]


def _parse_value(s: str) -> Any:
    if "-" in s[1:] or "–" in s:
        lo, hi = re.split(r"(?<=\d)[-–]", s, maxsplit=1)
        return [float(lo), float(hi)]
    return float(s)


def apply_decisions(entry: dict[str, Any], decisions: Mapping[str, Decision]) -> list[str]:
    """Apply decisions in place; returns the open questions created by ``defer``/``reject``."""
    questions: list[str] = []
    for path, d in decisions.items():
        container, key = _find(entry, path)
        q = container[key]
        if d.verdict == "reject":
            questions.append(f"{path}: rejected by reviewer ({d.note or 'no reason given'})")
            if isinstance(key, int):
                container.pop(key)
            else:
                del container[key]
            continue
        if d.value:
            q["value"] = _parse_value(d.value)
        q["reviewer_note"] = d.text
        if d.verdict == "defer":
            questions.append(f"{path}: deferred by reviewer ({d.note or 'no reason given'})")
    if questions:
        entry.setdefault("open_questions", [])
        entry["open_questions"] += [x for x in questions if x not in entry["open_questions"]]
    return questions


def approve(
    entry_path: Path, sheet_path: Path, *, reviewer: str, now: dt.datetime | None = None
) -> dict[str, Any]:
    """The gate. Raises ``ValueError`` listing every ⚠ row without a decision."""
    with entry_path.open() as f:
        entry = yaml.safe_load(f)
    text = sheet_path.read_text()
    decisions = parse_decisions(text)
    missing = sorted(warn_rows(text) - set(decisions))
    if missing:
        raise ValueError("decision required on: " + ", ".join(missing))
    low_without_note = [
        p
        for _s, p, q in _quantities(entry)
        if q.get("confidence") == "low" and p not in decisions and not q.get("reviewer_note")
    ]
    if low_without_note:
        raise ValueError("confidence: low without reviewer note: " + ", ".join(low_without_note))
    questions = apply_decisions(entry, decisions)
    stamp = (now or dt.datetime.now(dt.UTC)).replace(microsecond=0).isoformat()
    entry["reviewed_by"] = reviewer
    entry["reviewed_at"] = stamp
    entry["review_status"] = "reviewed"
    contributors = entry.setdefault("contributors", [])
    if not any(c.get("name") == reviewer and c.get("role") == "reviewer" for c in contributors):
        contributors.append({"name": reviewer, "role": "reviewer"})
    validate_entry(entry, file=entry_path)
    dump_entry(entry, entry_path)
    return {"decisions": len(decisions), "open_questions": questions, "reviewed_by": reviewer}


__all__ = [
    "Decision",
    "Row",
    "SheetInput",
    "apply_decisions",
    "approve",
    "parse_decisions",
    "render_sheet",
    "rows_for",
    "warn_rows",
]
