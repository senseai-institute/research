# SPDX-License-Identifier: Apache-2.0
"""Find a quote in a fetched document and return it with its surrounding context.

Texts are stored normalised the same way curate normalises them (``curate.fetch.normalise``:
runs of whitespace become one space), so the quote check here is the same as
``curate.extract.check_claim``'s, plus character offsets and the section the quote sits in.
A passage only exists if this code found it (N2).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

CONTEXT = 600


def normalise(text: str) -> str:
    """Identical to ``senseai_profiles.curate.fetch.normalise``."""
    return re.sub(r"\s+", " ", text).strip()


_DASHES = str.maketrans({"‐": "-", "‑": "-", "‒": "-", "–": "-", "—": "-", "−": "-"})


def _loose(text: str) -> str:
    """Lenient form used only to *find* a quote: unicode dashes and quotes folded."""
    return text.translate(_DASHES).replace("“", '"').replace("”", '"').replace("’", "'")


@dataclass(frozen=True, slots=True)
class Located:
    char_start: int
    char_end: int
    text: str  # the verbatim slice of the stored text
    section: str
    context_before: str
    context_after: str


def section_at(sections: list[tuple[str, int]], offset: int) -> str:
    title = ""
    for name, start in sorted(sections, key=lambda s: s[1]):
        if start <= offset:
            title = name
        else:
            break
    return title


def locate(
    text: str,
    quote: str,
    sections: list[tuple[str, int]] | None = None,
    *,
    around_char: int | None = None,
) -> Located | None:
    """Find ``quote`` in ``text`` (both normalised). ``None`` if it is not there.

    Matching folds unicode dashes and curly quotes, which do not change the length, so the
    offsets point into ``text`` and the returned slice is the document's own characters.
    When the quote occurs more than once, the occurrence nearest ``around_char`` wins.
    """
    q = _loose(normalise(quote))
    if not q:
        return None
    hay = _loose(text)
    starts: list[int] = []
    i = hay.find(q)
    while i >= 0:
        starts.append(i)
        i = hay.find(q, i + 1)
    if not starts:
        return None
    start = (
        min(starts, key=lambda s: abs(s - around_char)) if around_char is not None else starts[0]
    )
    end = start + len(q)
    return Located(
        char_start=start,
        char_end=end,
        text=text[start:end],
        section=section_at(sections or [], start),
        context_before=text[max(0, start - CONTEXT) : start],
        context_after=text[end : end + CONTEXT],
    )


def window(
    text: str, around_char: int, sections: list[tuple[str, int]] | None = None, size: int = 400
) -> Located:
    """The text around a character offset (no quote needed)."""
    a = max(0, min(len(text), around_char) - size // 2)
    b = min(len(text), a + size)
    return Located(
        a,
        b,
        text[a:b],
        section_at(sections or [], a),
        text[max(0, a - CONTEXT) : a],
        text[b : b + CONTEXT],
    )


__all__ = ["CONTEXT", "Located", "locate", "normalise", "section_at", "window"]
