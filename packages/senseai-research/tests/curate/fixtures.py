"""Canned API responses shaped like the real services, served through httpx.MockTransport.

These are hand-made cassettes: the shapes follow the public API docs, the
numbers are those of the papers the library already cites. The curator never
sees the network in tests.
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx

DOI_KANG = "10.1152/jn.90641.2008"
DOI_STEINBERG = "10.1002/cne.901480209"
DOI_GUNTER = "10.1113/jphysiol.1951.sp004599"
DOI_REVIEW = "10.9999/review.2020"
DOI_RETRACTED = "10.9999/retracted.2015"

KANG_ABSTRACT = (
    "We measured contrast sensitivity of cats and humans in scotopic and mesopic conditions. "
    "Cats had roughly 0.74 log unit greater absolute sensitivity than humans for spatial "
    "frequencies at or below 1/8 cpd. The advantage was largely explained by the greater "
    "light-collecting ability of their optics."
)
KANG_FULLTEXT = (
    "Abstract. "
    + KANG_ABSTRACT
    + "\n\nResults. Across the four cats the threshold illuminance at 1/8 cpd "
    "was 0.74 log units below the human value (n = 3 observers). Flicker fusion was not measured here. "
    "Methods. Stimuli were presented at 0.001 to 1 cd/m2 after 45 min dark adaptation.\n"
)
STEINBERG_ABSTRACT = (
    "Rods were more numerous than cones in every region studied. Their maximum density was about "
    "460,000/mm2 at an eccentricity of 10-15 degrees, forming a ring around the central cone peak. "
    "Cone density in the area centralis peaked at 26,000-27,000/mm2 and fell to 4000/mm2 in the periphery."
)
GUNTER_ABSTRACT = (
    "The absolute threshold for vision in the cat was measured behaviourally. The cat's threshold "
    "was about one log unit below the human threshold under the same conditions."
)
REVIEW_ABSTRACT = (
    "This review summarises feline vision. As Kang et al. (2009) reported, cats are 0.74 log units "
    "more sensitive than humans in scotopic conditions. Rod density peaks near 460,000 per mm2 "
    "(Steinberg et al., 1973)."
)
RETRACTED_ABSTRACT = "Cat flicker fusion frequency was 120 Hz in our behavioural paradigm."


def _openalex_work(
    doi: str,
    title: str,
    abstract: str,
    year: int,
    *,
    cited: int = 50,
    pmcid: str | None = None,
    refs: list[str] | None = None,
    is_review: bool = False,
) -> dict[str, Any]:
    words = abstract.split()
    inv: dict[str, list[int]] = {}
    for i, w in enumerate(words):
        inv.setdefault(w, []).append(i)
    return {
        "id": f"https://openalex.org/W{abs(hash(doi)) % 10**8}",
        "doi": f"https://doi.org/{doi}",
        "title": title,
        "publication_year": year,
        "type": "review" if is_review else "article",
        "cited_by_count": cited,
        "abstract_inverted_index": inv,
        "primary_location": {
            "source": {
                "display_name": "J Test",
                "is_in_doaj": True,
                "id": "https://openalex.org/S1",
            },
            "is_oa": pmcid is not None,
        },
        "open_access": {
            "is_oa": pmcid is not None,
            "oa_url": f"https://europepmc.org/article/PMC/{pmcid}" if pmcid else None,
        },
        "ids": {
            "doi": f"https://doi.org/{doi}",
            **({"pmcid": f"https://www.ncbi.nlm.nih.gov/pmc/articles/{pmcid}"} if pmcid else {}),
        },
        "referenced_works": [f"https://openalex.org/W{abs(hash(r)) % 10**8}" for r in (refs or [])],
    }


PAPERS: dict[str, dict[str, Any]] = {
    DOI_KANG: _openalex_work(
        DOI_KANG,
        "Contrast sensitivity of cats and humans in scotopic and mesopic conditions",
        KANG_ABSTRACT,
        2009,
        cited=80,
        pmcid="PMC2724355",
    ),
    DOI_STEINBERG: _openalex_work(
        DOI_STEINBERG,
        "The distribution of rods and cones in the retina of the cat",
        STEINBERG_ABSTRACT,
        1973,
        cited=400,
    ),
    DOI_GUNTER: _openalex_work(
        DOI_GUNTER, "The absolute threshold for vision in the cat", GUNTER_ABSTRACT, 1951, cited=120
    ),
    DOI_REVIEW: _openalex_work(
        DOI_REVIEW,
        "Feline vision: a review",
        REVIEW_ABSTRACT,
        2020,
        cited=5,
        refs=[DOI_KANG, DOI_STEINBERG],
        is_review=True,
    ),
    DOI_RETRACTED: _openalex_work(
        DOI_RETRACTED, "Flicker fusion in cats", RETRACTED_ABSTRACT, 2015, cited=2
    ),
}
ABSTRACTS = {
    DOI_KANG: KANG_ABSTRACT,
    DOI_STEINBERG: STEINBERG_ABSTRACT,
    DOI_GUNTER: GUNTER_ABSTRACT,
    DOI_REVIEW: REVIEW_ABSTRACT,
    DOI_RETRACTED: RETRACTED_ABSTRACT,
}

EUROPEPMC_XML = f"""<?xml version="1.0"?><article><front><article-meta><title-group><article-title>Contrast sensitivity of cats and humans</article-title></title-group>
<abstract><p>{KANG_ABSTRACT}</p></abstract></article-meta></front>
<body><sec><title>Results</title><p>Across the four cats the threshold illuminance at 1/8 cpd was 0.74 log units below the human value (n = 3 observers). Flicker fusion was not measured here.</p></sec>
<sec><title>Methods</title><p>Stimuli were presented at 0.001 to 1 cd/m2 after 45 min dark adaptation.</p></sec></body></article>"""


def handler(request: httpx.Request) -> httpx.Response:
    u = urlparse(str(request.url))
    q = {k: v[0] for k, v in parse_qs(u.query).items()}
    host, path = u.netloc, u.path
    # --- taxonomy ---------------------------------------------------------------
    if host == "api.gbif.org":
        if path.endswith("/species/match"):
            if q.get("name", "").lower() == "felidae":
                return httpx.Response(
                    200,
                    json={
                        "usageKey": 9703,
                        "scientificName": "Felidae",
                        "canonicalName": "Felidae",
                        "rank": "FAMILY",
                        "matchType": "EXACT",
                    },
                )
            return httpx.Response(200, json={"matchType": "NONE"})
        if path.endswith("/synonyms"):
            return httpx.Response(200, json={"results": [{"canonicalName": "Felinae"}]})
        if path.endswith("/vernacularNames"):
            return httpx.Response(
                200,
                json={
                    "results": [
                        {"vernacularName": "Cats", "language": "eng"},
                        {"vernacularName": "Katzen", "language": "deu"},
                        {"vernacularName": "cats", "language": "eng"},
                    ]
                },
            )
        if path.endswith("/children"):
            key = path.split("/")[-2]
            if key == "9703":  # family -> genera (+ a subfamily row that must be skipped)
                return httpx.Response(
                    200,
                    json={
                        "results": [
                            {"key": 2435022, "canonicalName": "Felis", "rank": "GENUS"},
                            {"key": 2435194, "canonicalName": "Panthera", "rank": "GENUS"},
                            {"key": 1, "canonicalName": "Felinae", "rank": "SUBFAMILY"},
                        ]
                    },
                )
            if key == "2435022":
                return httpx.Response(
                    200,
                    json={
                        "results": [
                            {"key": 2435035, "canonicalName": "Felis catus", "rank": "SPECIES"},
                            {
                                "key": 2435040,
                                "canonicalName": "Felis silvestris",
                                "rank": "SPECIES",
                            },
                        ]
                    },
                )
            if key == "2435194":
                return httpx.Response(
                    200,
                    json={
                        "results": [
                            {"key": 5219436, "canonicalName": "Panthera pardus", "rank": "SPECIES"}
                        ]
                    },
                )
            return httpx.Response(200, json={"results": []})
    if host == "api.opentreeoflife.org":
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "name": "Felidae",
                        "matches": [
                            {
                                "taxon": {
                                    "ott_id": 563159,
                                    "unique_name": "Felidae",
                                    "synonyms": ["Felidae Fischer de Waldheim, 1817"],
                                }
                            }
                        ],
                    }
                ]
            },
        )
    if host == "api.checklistbank.org":
        if path.endswith("/nameusage/search"):
            return httpx.Response(
                200,
                json={
                    "result": [
                        {
                            "id": "6F5GH",
                            "usage": {"id": "6F5GH", "name": {"scientificName": "Felidae"}},
                        }
                    ]
                },
            )
        if path.endswith("/vernacular"):
            return httpx.Response(
                200,
                json=[{"name": "cats", "language": "eng"}, {"name": "felids", "language": "eng"}],
            )
    if host == "api.iucnredlist.org":
        return httpx.Response(
            200,
            json={
                "assessments": [
                    {
                        "latest": True,
                        "red_list_category_code": "VU",
                        "assessment_id": 123,
                        "sis_taxon_id": 15954,
                        "url": "https://www.iucnredlist.org/species/15954/123",
                    }
                ]
            },
        )
    if host == "query.wikidata.org":
        return httpx.Response(
            200,
            json={
                "results": {
                    "bindings": [
                        {
                            "item": {"value": "http://www.wikidata.org/entity/Q25265"},
                            "gbif": {"value": "9703"},
                        }
                    ]
                }
            },
        )
    # --- search ------------------------------------------------------------------
    if host == "api.openalex.org" and path == "/works":
        search = q.get("search", "").lower()
        hits = [
            w for d, w in PAPERS.items() if any(t in search for t in ("cat", "felidae", "felis"))
        ]
        if "flicker" in search:
            hits = [PAPERS[DOI_RETRACTED], PAPERS[DOI_REVIEW]]
        elif "rod" in search or "density" in search:
            hits = [PAPERS[DOI_STEINBERG], PAPERS[DOI_REVIEW]]
        elif "threshold" in search or "sensitivity" in search:
            hits = [PAPERS[DOI_KANG], PAPERS[DOI_GUNTER], PAPERS[DOI_REVIEW]]
        else:
            hits = hits[:3]
        return httpx.Response(200, json={"meta": {"count": len(hits)}, "results": hits})
    if host == "api.openalex.org" and path.startswith("/works/"):
        doi = path.split("/works/", 1)[1].replace("https://doi.org/", "")
        w = PAPERS.get(doi)
        return (
            httpx.Response(200, json=w) if w else httpx.Response(404, json={"error": "not found"})
        )
    if host == "api.semanticscholar.org":
        if "/paper/search" in path:
            return httpx.Response(
                200,
                json={
                    "total": 1,
                    "data": [
                        {
                            "paperId": "abc",
                            "externalIds": {"DOI": DOI_KANG},
                            "title": PAPERS[DOI_KANG]["title"],
                            "abstract": KANG_ABSTRACT,
                            "year": 2009,
                            "citationCount": 80,
                        }
                    ],
                },
            )
        if "/citations" in path:
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "intents": ["background"],
                            "isInfluential": False,
                            "citingPaper": {"paperId": "r1", "title": "Feline vision: a review"},
                        },
                        {
                            "intents": ["result"],
                            "isInfluential": True,
                            "citingPaper": {"paperId": "r2", "title": "A supporting paper"},
                        },
                    ]
                },
            )
        return httpx.Response(200, json={"paperId": "abc", "externalIds": {"DOI": DOI_KANG}})
    if host == "eutils.ncbi.nlm.nih.gov":
        if path.endswith("esearch.fcgi"):
            return httpx.Response(
                200, json={"esearchresult": {"count": "1", "idlist": ["19339460"]}}
            )
        if path.endswith("esummary.fcgi"):
            return httpx.Response(
                200,
                json={
                    "result": {
                        "uids": ["19339460"],
                        "19339460": {
                            "uid": "19339460",
                            "title": PAPERS[DOI_KANG]["title"],
                            "pubdate": "2009 Aug",
                            "articleids": [
                                {"idtype": "doi", "value": DOI_KANG},
                                {"idtype": "pmc", "value": "PMC2724355"},
                            ],
                        },
                    }
                },
            )
    if host == "www.ebi.ac.uk":
        if path.endswith("/rest/search"):
            return httpx.Response(
                200,
                json={
                    "hitCount": 2,
                    "resultList": {
                        "result": [
                            {
                                "doi": DOI_KANG,
                                "pmcid": "PMC2724355",
                                "title": PAPERS[DOI_KANG]["title"],
                                "abstractText": KANG_ABSTRACT,
                                "pubYear": "2009",
                                "isOpenAccess": "Y",
                            },
                            {
                                "doi": DOI_STEINBERG,
                                "title": PAPERS[DOI_STEINBERG]["title"],
                                "abstractText": STEINBERG_ABSTRACT,
                                "pubYear": "1973",
                                "isOpenAccess": "N",
                            },
                        ]
                    },
                },
            )
        if "/fullTextXML" in path:
            return httpx.Response(
                200, text=EUROPEPMC_XML, headers={"content-type": "application/xml"}
            )
    if host == "api.biorxiv.org":
        return httpx.Response(200, json={"collection": []})
    # --- fetch -------------------------------------------------------------------
    if host == "api.unpaywall.org":
        doi = path.split("/v2/", 1)[1]
        if doi == DOI_KANG:
            return httpx.Response(
                200,
                json={
                    "is_oa": True,
                    "best_oa_location": {
                        "url_for_pdf": None,
                        "url": "https://europepmc.org/article/PMC/PMC2724355",
                        "host_type": "repository",
                    },
                },
            )
        return httpx.Response(200, json={"is_oa": False, "best_oa_location": None})
    if host == "api.core.ac.uk":
        return httpx.Response(200, json={"results": []})
    if host == "export.arxiv.org":
        return httpx.Response(200, text="<feed></feed>")
    # --- vet ---------------------------------------------------------------------
    if host == "api.crossref.org" and path.startswith("/works/"):
        doi = path.split("/works/", 1)[1]
        if doi == DOI_RETRACTED:
            return httpx.Response(
                200,
                json={
                    "message": {
                        "DOI": doi,
                        "update-to": [
                            {
                                "type": "retraction",
                                "DOI": "10.9999/retraction.notice",
                                "updated": {"date-time": "2021-03-02T00:00:00Z"},
                            }
                        ],
                        "reference": [],
                    }
                },
            )
        if doi == DOI_REVIEW:
            return httpx.Response(
                200,
                json={
                    "message": {
                        "DOI": doi,
                        "reference": [
                            {"DOI": DOI_KANG, "key": "r1"},
                            {"DOI": DOI_STEINBERG, "key": "r2"},
                        ],
                    }
                },
            )
        if doi in PAPERS:
            return httpx.Response(
                200, json={"message": {"DOI": doi, "reference": [], "container-title": ["J Test"]}}
            )
        return httpx.Response(404, json={"status": "error"})
    if host == "pubpeer.com":
        doi = q.get("doi", "")
        if doi == DOI_GUNTER:
            return httpx.Response(
                200,
                json={
                    "publications": [
                        {"doi": doi, "url": "https://pubpeer.com/publications/ABC", "comments": 1}
                    ]
                },
            )
        return httpx.Response(200, json={"publications": []})
    return httpx.Response(404, text=f"unhandled {request.url}")


def transport() -> httpx.MockTransport:
    return httpx.MockTransport(handler)


def dump(obj: Any) -> str:
    return json.dumps(obj, indent=1, sort_keys=True)
