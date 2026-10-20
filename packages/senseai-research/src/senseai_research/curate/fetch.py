# SPDX-License-Identifier: Apache-2.0
"""Fetch paper text: open-access full text where it exists, else the abstract.

Order: Europe PMC full-text XML (needs a PMCID), then Unpaywall's best OA
location (PDF or HTML, only on hosts we know how to read), then CORE, then
arXiv. Anything paywalled is skipped: the abstract from the search record is
used and the result is marked ``fulltext=False``. No publisher site is ever
scraped.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any

from defusedxml import ElementTree

from senseai_research.curate.cache import CachedClient, CacheMiss
from senseai_research.curate.search import Paper

EUROPEPMC_FT = "https://www.ebi.ac.uk/europepmc/webservices/rest/{pmcid}/fullTextXML"
UNPAYWALL = "https://api.unpaywall.org/v2/{doi}"
CORE = "https://api.core.ac.uk/v3/search/works"
ARXIV = "https://export.arxiv.org/api/query"
OA_HOSTS = (
    "europepmc.org",
    "www.ncbi.nlm.nih.gov",
    "arxiv.org",
    "biorxiv.org",
    "www.biorxiv.org",
    "core.ac.uk",
)


@dataclass(frozen=True, slots=True)
class PaperText:
    doi: str
    text: str
    fulltext: bool
    source: str  # europepmc | unpaywall | core | arxiv | abstract | none
    sections: tuple[tuple[str, int], ...] = ()  # (title, char offset) for page_or_section


def normalise(text: str) -> str:
    """Whitespace normalisation used identically for sent text and quote checks."""
    return re.sub(r"\s+", " ", text).strip()


def _xml_text(xml: str) -> tuple[str, tuple[tuple[str, int], ...]]:
    root = ElementTree.fromstring(xml.encode() if isinstance(xml, str) else xml)
    parts: list[str] = []
    sections: list[tuple[str, int]] = []

    def walk(el: Any, title: str) -> None:
        tag = el.tag.split("}")[-1]
        if tag == "sec":
            t = el.find("title")
            title = "".join(t.itertext()).strip() if t is not None else title
            sections.append((title, len(" ".join(parts))))
        if tag == "abstract":
            sections.append(("abstract", len(" ".join(parts))))
            title = "abstract"
        if tag in ("p", "title", "td", "th", "label", "caption"):
            txt = normalise("".join(el.itertext()))
            if txt:
                parts.append(txt)
            return
        for child in el:
            walk(child, title)

    walk(root, "")
    return " ".join(parts), tuple(sections)


def europepmc_fulltext(client: CachedClient, paper: Paper) -> PaperText | None:
    if not paper.pmcid:
        return None
    r = client.get(EUROPEPMC_FT.format(pmcid=paper.pmcid))
    if not r.ok or not r.text.lstrip().startswith("<"):
        return None
    try:
        text, sections = _xml_text(r.text)
    except ElementTree.ParseError:
        return None
    return (
        PaperText(paper.doi, normalise(text), True, "europepmc", sections) if text.strip() else None
    )


def unpaywall_fulltext(client: CachedClient, paper: Paper) -> PaperText | None:
    email = client.email or os.environ.get("UNPAYWALL_EMAIL", "")
    if not email:
        return None
    r = client.get(UNPAYWALL.format(doi=paper.doi), params={"email": email})
    if not r.ok:
        return None
    loc = (r.json() or {}).get("best_oa_location") or {}
    url = loc.get("url_for_pdf") or loc.get("url")
    if not url or not any(h in url for h in OA_HOSTS):
        return None
    if "europepmc.org/article/PMC/" in url:
        pmcid = url.rsplit("/", 1)[-1]
        return europepmc_fulltext(client, Paper(paper.doi, paper.title, pmcid=pmcid))
    body = client.get(url)
    if not body.ok:
        return None
    if url.lower().endswith(".pdf") or "application/pdf" in body.headers.get("content-type", ""):
        return None  # binary PDFs need the raw bytes; the cache stores text. Left for the reviewer.
    text = normalise(re.sub(r"<[^>]+>", " ", body.text))
    return PaperText(paper.doi, text, True, "unpaywall") if len(text) > 500 else None


def core_fulltext(client: CachedClient, paper: Paper) -> PaperText | None:
    key = os.environ.get("CORE_API_KEY", "")
    if not key:
        return None
    r = client.get(
        CORE,
        params={"q": f'doi:"{paper.doi}"', "limit": 1},
        headers={"Authorization": f"Bearer {key}"},
    )
    if not r.ok:
        return None
    for w in (r.json() or {}).get("results", []):
        ft = w.get("fullText")
        if ft and len(ft) > 500:
            return PaperText(paper.doi, normalise(ft), True, "core")
    return None


def arxiv_fulltext(client: CachedClient, paper: Paper) -> PaperText | None:
    if not paper.doi.startswith("10.48550/arxiv."):
        return None
    aid = paper.doi.split("arxiv.", 1)[1]
    r = client.get(ARXIV, params={"id_list": aid})
    if not r.ok:
        return None
    m = re.search(r"<summary>(.*?)</summary>", r.text, re.S)
    return PaperText(paper.doi, normalise(m.group(1)), False, "arxiv") if m else None


def fetch_text(client: CachedClient, paper: Paper) -> PaperText:
    """Best available text for a paper, never paywalled content."""
    for fn in (europepmc_fulltext, unpaywall_fulltext, core_fulltext, arxiv_fulltext):
        try:
            got = fn(client, paper)
        except CacheMiss:
            got = None
        except Exception:  # a flaky source falls through to the next
            got = None
        if got is not None:
            return got
    if paper.abstract:
        return PaperText(
            paper.doi, normalise(paper.abstract), False, "abstract", (("abstract", 0),)
        )
    return PaperText(paper.doi, "", False, "none")


__all__ = [
    "PaperText",
    "arxiv_fulltext",
    "core_fulltext",
    "europepmc_fulltext",
    "fetch_text",
    "normalise",
    "unpaywall_fulltext",
]
