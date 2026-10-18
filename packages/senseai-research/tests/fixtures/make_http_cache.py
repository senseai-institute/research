# SPDX-License-Identifier: Apache-2.0
"""Rebuild ``tests/fixtures/http-cache``: curate cache entries for the synthetic Kang fixture.

Two requests are cached, exactly as curate makes them: the Europe PMC search for the DOI (which
gives the PMCID) and the Europe PMC full-text XML (``papers/PMC2724355.xml``, synthetic). Run:

    python tests/fixtures/make_http_cache.py
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from senseai_profiles.curate.cache import CachedClient, CachedResponse, cache_key
from senseai_profiles.curate.fetch import EUROPEPMC_FT
from senseai_profiles.curate.search import EUROPEPMC

HERE = Path(__file__).parent
DOI = "10.1152/jn.90641.2008"
PMCID = "PMC2724355"


def main() -> None:
    root = HERE / "http-cache"
    shutil.rmtree(root, ignore_errors=True)
    c = CachedClient(root, offline=True)
    search_params = {
        "query": f'DOI:"{DOI}"',
        "format": "json",
        "pageSize": 1,
        "resultType": "lite",
    }
    search = {
        "resultList": {
            "result": [
                {
                    "doi": DOI,
                    "title": "Contrast sensitivity of cats and humans in scotopic and mesopic "
                    "conditions",
                    "pubYear": "2009",
                    "journalTitle": "J Neurophysiol",
                    "pmcid": PMCID,
                    "pmid": "19553478",
                    "isOpenAccess": "Y",
                    "citedByCount": 25,
                    "pubType": "research-article; journal article",
                }
            ]
        }
    }
    entries = [
        ("GET", EUROPEPMC, search_params, json.dumps(search), "application/json"),
        (
            "GET",
            EUROPEPMC_FT.format(pmcid=PMCID),
            None,
            (HERE / "papers" / f"{PMCID}.xml").read_text(),
            "application/xml",
        ),
    ]
    for method, url, params, body, ctype in entries:
        key = cache_key(method, url, params, None)
        c._store(
            key,
            CachedResponse(
                url, 200, body, {"content-type": ctype}, "2026-10-17T00:00:00Z", False, key
            ),
        )
    c.close()
    print(f"wrote {len(entries)} entries under {root}")


if __name__ == "__main__":
    main()
