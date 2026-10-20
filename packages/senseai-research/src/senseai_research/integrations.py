# SPDX-License-Identifier: Apache-2.0
"""Every outside service senseai-research talks to, the variable that holds its key, and a live
check that the key works.

Keys come from the environment: Railway service variables, or ``~/senseai/research.env`` on the
EC2 box. This module reports only whether a variable is set. A key's value is never returned,
logged or stored, and check results are sentences written here, never upstream error text
(which can echo the request URL, key included).

``docs/APIS.md`` is generated from this list (``senseai-research api-doc``).
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

import httpx

# The addresses curate calls. Copied rather than imported, because curate needs the private
# senseai-profiles package and this list must load without it; a test checks they match.
ARXIV = "https://export.arxiv.org/api/query"
COL = "https://api.checklistbank.org/dataset/3LR"
CORE = "https://api.core.ac.uk/v3/search/works"
CROSSREF = "https://api.crossref.org/works/{doi}"
EUROPEPMC = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
GBIF = "https://api.gbif.org/v1"
IUCN = "https://api.iucnredlist.org/api/v4"
OPENALEX = "https://api.openalex.org/works"
PUBMED = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
PUBPEER = "https://pubpeer.com/api/v2/publications"
S2 = "https://api.semanticscholar.org/graph/v1/paper/search"
UNPAYWALL = "https://api.unpaywall.org/v2/{doi}"
WIKIDATA = "https://query.wikidata.org/sparql"

Need = Literal["required", "recommended", "optional", "none"]
TEST_DOI = "10.1152/jn.90641.2008"  # the §11 paper; open access, so every service knows it


@dataclass(frozen=True)
class Var:
    name: str
    secret: bool = True
    aliases: tuple[str, ...] = ()

    def value(self, env: Mapping[str, str]) -> str:
        for n in (self.name, *self.aliases):
            if env.get(n):
                return env[n]
        return ""


@dataclass(frozen=True)
class Result:
    ok: bool
    detail: str


Check = Callable[[Mapping[str, str], httpx.Client], Result]


@dataclass(frozen=True)
class Integration:
    id: str
    name: str
    group: str
    purpose: str
    need: Need
    without: str
    get_key: str
    vars: tuple[Var, ...] = ()
    check: Check | None = field(default=None, compare=False)
    # True when the service also answers without the key (a smaller shared allowance), so the
    # check is worth running before a key is set.
    works_without_key: bool = False

    def state(self, env: Mapping[str, str]) -> str:
        """``no_key`` (nothing to set), ``set``, ``partial`` or ``not_set``."""
        if not self.vars:
            return "no_key"
        have = [bool(v.value(env)) for v in self.vars]
        return "set" if all(have) else "partial" if any(have) else "not_set"


CONTACT = Var("SENSEAI_CONTACT_EMAIL", secret=False, aliases=("UNPAYWALL_EMAIL",))


def probe(
    method: str,
    url: str,
    *,
    params: Callable[[Mapping[str, str]], dict[str, Any]] | None = None,
    headers: Callable[[Mapping[str, str]], dict[str, str]] | None = None,
    body: dict[str, Any] | None = None,
) -> Check:
    """A check that makes one small request and judges it by its HTTP status."""

    def run(env: Mapping[str, str], http: httpx.Client) -> Result:
        try:
            r = http.request(
                method,
                url,
                params=params(env) if params else None,
                headers=headers(env) if headers else None,
                json=body,
            )
        except httpx.HTTPError as e:
            return Result(False, f"could not connect ({type(e).__name__})")
        if r.is_success:
            return Result(True, "working")
        if r.status_code in (401, 403):
            return Result(False, "the key was refused (check that it is copied in full)")
        if r.status_code == 429:
            return Result(False, "reachable, but rate-limited right now; try again in a minute")
        return Result(False, f"unexpected answer (HTTP {r.status_code})")

    return run


def _anthropic(env: Mapping[str, str], http: httpx.Client) -> Result:
    from senseai_research.agent.client import make_client
    from senseai_research.config import DEFAULT_MODEL

    model = env.get("RESEARCH_MODEL") or DEFAULT_MODEL
    try:
        make_client().with_options(timeout=15.0, max_retries=0).models.retrieve(model)
    except Exception as e:  # any SDK error: auth, permission, network
        return Result(False, f"the model {model} is not reachable ({type(e).__name__})")
    return Result(True, f"working; model {model} is available")


def _github(env: Mapping[str, str], http: httpx.Client) -> Result:
    api = env.get("RESEARCH_GITHUB_API") or "https://api.github.com"
    repo = env.get("RESEARCH_GITHUB_REPO") or "senseai-institute/profiles"
    try:
        r = http.get(
            f"{api}/repos/{repo}",
            headers={"Authorization": f"Bearer {env['GITHUB_TOKEN']}"},
        )
    except httpx.HTTPError as e:
        return Result(False, f"could not connect ({type(e).__name__})")
    if r.is_success:
        return Result(True, f"working; the token can see {repo}")
    if r.status_code in (401, 403):
        return Result(False, "the token was refused")
    if r.status_code == 404:
        return Result(False, f"the token cannot see {repo}; give it access to that repository")
    return Result(False, f"unexpected answer (HTTP {r.status_code})")


def _s3(env: Mapping[str, str], http: httpx.Client) -> Result:
    url = env.get("RESEARCH_CURATE_S3", "")
    if not url.startswith("s3://"):
        return Result(False, "RESEARCH_CURATE_S3 must look like s3://bucket/prefix")
    try:
        import boto3
    except ImportError:
        return Result(False, "boto3 is not installed (install senseai-research[aws])")
    bucket = url[5:].partition("/")[0]
    try:
        boto3.client("s3").head_bucket(Bucket=bucket)
    except Exception as e:  # botocore errors: credentials, permission, missing bucket
        return Result(False, f"cannot reach bucket {bucket} ({type(e).__name__})")
    return Result(True, f"working; bucket {bucket} is reachable")


INTEGRATIONS: tuple[Integration, ...] = (
    Integration(
        "anthropic",
        "Anthropic (Claude)",
        "Assistant",
        "Runs the research assistant in the chat, and reads papers for the library drafting "
        "pipeline.",
        "required",
        "The chat cannot answer. Everything else (the ledger, impact previews, experiments, "
        "approvals) still works.",
        "https://console.anthropic.com/settings/keys",
        (Var("ANTHROPIC_API_KEY"),),
        _anthropic,
        works_without_key=True,  # the SDK also finds credentials outside ANTHROPIC_API_KEY
    ),
    Integration(
        "contact_email",
        "Contact email",
        "Finding papers",
        "Sent to OpenAlex, Crossref and Unpaywall so they put requests in their faster "
        "'polite' queue and can reach you if something goes wrong. Unpaywall needs it.",
        "recommended",
        "OpenAlex and Crossref answer more slowly, and Unpaywall is skipped when looking for "
        "free full text.",
        "Any address you read, such as a team inbox.",
        (CONTACT,),
    ),
    Integration(
        "openalex",
        "OpenAlex",
        "Finding papers",
        "The main paper search, plus citation counts and journal details used when vetting "
        "a paper.",
        "recommended",
        "Searches still run on OpenAlex's shared allowance, which is smaller and can run out "
        "during a long drafting run.",
        "https://openalex.org/settings/api (free account)",
        (Var("OPENALEX_API_KEY"),),
        probe(
            "GET",
            OPENALEX,
            params=lambda env: (
                {"search": "cat vision", "per-page": 1, "select": "id"}
                | ({"api_key": env["OPENALEX_API_KEY"]} if env.get("OPENALEX_API_KEY") else {})
            ),
        ),
        works_without_key=True,
    ),
    Integration(
        "semantic_scholar",
        "Semantic Scholar",
        "Finding papers",
        "A second paper search, and the reference lists used to cross-check papers.",
        "recommended",
        "Requests share Semantic Scholar's public allowance and are often refused when it is "
        "busy, so some searches come back short.",
        "https://www.semanticscholar.org/product/api#api-key-form",
        (Var("S2_API_KEY"),),
        probe(
            "GET",
            S2,
            params=lambda env: {"query": "cat vision", "limit": 1, "fields": "title"},
            headers=lambda env: {"x-api-key": env["S2_API_KEY"]} if env.get("S2_API_KEY") else {},
        ),
        works_without_key=True,
    ),
    Integration(
        "ncbi",
        "NCBI E-utilities (PubMed)",
        "Finding papers",
        "Searches PubMed.",
        "recommended",
        "PubMed allows 3 requests a second instead of 10, so searches are slower.",
        "https://account.ncbi.nlm.nih.gov/settings/ (API Key Management)",
        (Var("NCBI_API_KEY"),),
        probe(
            "GET",
            f"{PUBMED}/einfo.fcgi",
            params=lambda env: (
                {"db": "pubmed", "retmode": "json"}
                | ({"api_key": env["NCBI_API_KEY"]} if env.get("NCBI_API_KEY") else {})
            ),
        ),
        works_without_key=True,
    ),
    Integration(
        "europepmc",
        "Europe PMC",
        "Finding papers",
        "Searches Europe PMC and downloads open-access full text, the main source of the "
        "passages a biologist checks.",
        "none",
        "Nothing to set.",
        "No key needed.",
        (),
        probe(
            "GET", EUROPEPMC, params=lambda env: {"query": "cat", "format": "json", "pageSize": 1}
        ),
    ),
    Integration(
        "unpaywall",
        "Unpaywall",
        "Full text",
        "Finds a free, legal copy of a paper when Europe PMC does not have it.",
        "recommended",
        "Papers that are only free through Unpaywall stay abstract-only.",
        "No key: set SENSEAI_CONTACT_EMAIL (Contact email above).",
        (CONTACT,),
        probe(
            "GET",
            UNPAYWALL.format(doi=TEST_DOI),
            params=lambda env: {"email": CONTACT.value(env)},
        ),
    ),
    Integration(
        "core",
        "CORE",
        "Full text",
        "A further source of open-access full text, from university repositories.",
        "optional",
        "CORE is skipped; Europe PMC and Unpaywall are still tried.",
        "https://core.ac.uk/services/api#form (free)",
        (Var("CORE_API_KEY"),),
        probe(
            "GET",
            CORE,
            params=lambda env: {"q": f'doi:"{TEST_DOI}"', "limit": 1},
            headers=lambda env: {"Authorization": f"Bearer {env.get('CORE_API_KEY', '')}"},
        ),
    ),
    Integration(
        "arxiv",
        "arXiv",
        "Full text",
        "Preprints.",
        "none",
        "Nothing to set.",
        "No key needed.",
        (),
        probe("GET", ARXIV, params=lambda env: {"search_query": "all:retina", "max_results": 1}),
    ),
    Integration(
        "crossref",
        "Crossref",
        "Checking papers",
        "Confirms a DOI is real and matches the paper, and reports retractions and corrections.",
        "none",
        "Nothing to set (the contact email makes it faster).",
        "No key needed.",
        (),
        probe("GET", CROSSREF.format(doi=TEST_DOI)),
    ),
    Integration(
        "pubpeer",
        "PubPeer",
        "Checking papers",
        "Counts public post-publication comments on a paper, a warning sign worth a look.",
        "optional",
        "The PubPeer check is skipped; retractions are still checked through Crossref.",
        "https://pubpeer.com/static/contact (ask for an API key)",
        (Var("PUBPEER_API_KEY"),),
        probe(
            "GET",
            PUBPEER,
            params=lambda env: {"doi": TEST_DOI, "key": env.get("PUBPEER_API_KEY", "")},
        ),
    ),
    Integration(
        "gbif",
        "GBIF",
        "Species names",
        "Checks species names and lists the species in a clade.",
        "none",
        "Nothing to set.",
        "No key needed.",
        (),
        probe("GET", f"{GBIF}/species/match", params=lambda env: {"name": "Felis catus"}),
    ),
    Integration(
        "catalogue_of_life",
        "Catalogue of Life (ChecklistBank)",
        "Species names",
        "A second check of species names and synonyms.",
        "none",
        "Nothing to set.",
        "No key needed.",
        (),
        probe("GET", f"{COL}/nameusage/search", params=lambda env: {"q": "Felis", "limit": 1}),
    ),
    Integration(
        "wikidata",
        "Wikidata",
        "Species names",
        "Common names for species.",
        "none",
        "Nothing to set.",
        "No key needed.",
        (),
        probe(
            "GET",
            WIKIDATA,
            params=lambda env: {"query": "ASK {}", "format": "json"},
        ),
    ),
    Integration(
        "iucn",
        "IUCN Red List",
        "Species names",
        "Conservation status of each species, shown on the review sheet.",
        "optional",
        "Conservation status is left for the reviewer to fill in.",
        "https://api.iucnredlist.org/users/sign_up",
        (Var("IUCN_API_TOKEN"),),
        probe(
            "GET",
            f"{IUCN}/information/api_version",
            headers=lambda env: {"Authorization": env.get("IUCN_API_TOKEN", "")},
        ),
    ),
    Integration(
        "studio",
        "Studio connection",
        "Connection",
        "A shared secret Studio sends with every request, so the research server can trust "
        "the signed-in name Studio forwards. Set the same value on both services.",
        "optional",
        "The research server answers only callers on its own machine, so Studio must run "
        "beside it. Required when Studio reaches it over a network, as on Railway.",
        "Any long random string, e.g. the output of: openssl rand -hex 32",
        (Var("RESEARCH_PROXY_TOKEN"),),
    ),
    Integration(
        "github",
        "GitHub",
        "Library changes",
        "Opens the pull request on senseai-institute/profiles after a person approves a "
        "library change. Use a fine-grained token with Contents and Pull requests "
        "(read and write) on that one repository only.",
        "optional",
        "Approved changes are saved as a patch and a pull-request text to download, and "
        "someone opens the pull request by hand.",
        "https://github.com/settings/personal-access-tokens/new",
        (Var("GITHUB_TOKEN"),),
        _github,
    ),
    Integration(
        "s3",
        "Amazon S3",
        "Backups",
        "Copies ledger backups and the download cache to a bucket, so nothing is lost if the "
        "server's disk is.",
        "optional",
        "Backups stay on the server's own disk.",
        "AWS console: an IAM user with read and write on one bucket prefix.",
        (
            Var("RESEARCH_CURATE_S3", secret=False),
            Var("AWS_ACCESS_KEY_ID"),
            Var("AWS_SECRET_ACCESS_KEY"),
        ),
        _s3,
    ),
)

BY_ID = {i.id: i for i in INTEGRATIONS}
GROUPS = tuple(dict.fromkeys(i.group for i in INTEGRATIONS))


def status(env: Mapping[str, str] | None = None) -> list[dict[str, Any]]:
    """What the settings page shows: each service, whether its variables are set, never values."""
    env = os.environ if env is None else env
    return [
        {
            "id": i.id,
            "name": i.name,
            "group": i.group,
            "purpose": i.purpose,
            "need": i.need,
            "without": i.without,
            "get_key": i.get_key,
            "state": i.state(env),
            "vars": [
                {
                    "name": v.name,
                    "aliases": list(v.aliases),
                    "secret": v.secret,
                    "set": bool(v.value(env)),
                }
                for v in i.vars
            ],
            "checkable": i.check is not None,
        }
        for i in INTEGRATIONS
    ]


def check(
    integration_id: str,
    env: Mapping[str, str] | None = None,
    *,
    http: httpx.Client | None = None,
) -> Result:
    """Try the service once with the configured key. Unknown ids raise ``KeyError``."""
    i = BY_ID[integration_id]
    env = os.environ if env is None else env
    if i.check is None:
        return Result(False, "no live check for this service")
    keyless = i.state(env) in ("not_set", "partial")
    if keyless and not i.works_without_key:
        missing = ", ".join(v.name for v in i.vars if not v.value(env))
        return Result(False, f"not set: {missing}")
    own = http is None
    client = http or httpx.Client(timeout=15.0, follow_redirects=True)
    try:
        r = i.check(env, client)
    finally:
        if own:
            client.close()
    if r.ok and keyless:
        return Result(True, f"{r.detail}, without a key (shared allowance)")
    return r


class CheckCache:
    """Remembers each result for ``ttl`` seconds, so a busy settings page does not hammer APIs."""

    def __init__(self, ttl: float = 30.0) -> None:
        self.ttl = ttl
        self._seen: dict[str, tuple[float, Result]] = {}

    def run(self, integration_id: str, **kw: Any) -> tuple[Result, bool]:
        hit = self._seen.get(integration_id)
        if hit and time.monotonic() - hit[0] < self.ttl:
            return hit[1], True
        r = check(integration_id, **kw)
        self._seen[integration_id] = (time.monotonic(), r)
        return r, False


def env_template() -> str:
    """Every variable, grouped, for Railway's raw variable editor or ``research.env``."""
    out: list[str] = []
    done: set[str] = set()
    for g in GROUPS:
        names: list[str] = []
        for i in INTEGRATIONS:
            for v in i.vars:
                if i.group == g and v.name not in done:
                    done.add(v.name)
                    names.append(v.name)
        if names:
            out += [f"# {g}", *(f"{n}=" for n in names), ""]
    return "\n".join(out)


NEED_LABEL = {
    "required": "Required",
    "recommended": "Recommended",
    "optional": "Optional",
    "none": "No key needed",
}


def render_doc() -> str:
    """``docs/APIS.md``: the list of services and keys, written from this module."""
    out = [
        "# Outside services and API keys",
        "",
        "<!-- Generated by `senseai-research api-doc` from"
        " `src/senseai_research/integrations.py`. Do not edit by hand. -->",
        "",
        "Every outside service senseai-research uses, and the environment variable that holds",
        "its key. Keys are set on the research server, never in the app:",
        "",
        "- **Railway:** the research service, *Variables* tab. Paste the block at the end into",
        "  *Raw Editor* and fill in the values; Railway redeploys when you save.",
        "- **EC2 box:** `~/senseai/research.env`, then `research-ctl.sh restart`.",
        "",
        "The *Settings* tab of the Research page (and `senseai-research settings --check`) shows",
        "which keys are set and tests each one. It never shows a key's value.",
        "",
        "| Service | Needed? | Variable | Used for |",
        "|---|---|---|---|",
    ]
    for i in INTEGRATIONS:
        names = ", ".join(f"`{v.name}`" for v in i.vars) or "none"
        out.append(f"| {i.name} | {NEED_LABEL[i.need]} | {names} | {i.purpose} |")
    for g in GROUPS:
        out += ["", f"## {g}"]
        for i in INTEGRATIONS:
            if i.group != g:
                continue
            out += ["", f"### {i.name}", "", i.purpose, ""]
            out.append(f"- **Needed:** {NEED_LABEL[i.need]}.")
            for v in i.vars:
                alias = f" (or `{'`, `'.join(v.aliases)}`)" if v.aliases else ""
                kind = "secret" if v.secret else "not secret"
                out.append(f"- **Variable:** `{v.name}`{alias}, {kind}.")
            out.append(f"- **Without it:** {i.without}")
            out.append(f"- **Get a key:** {i.get_key}")
    out += [
        "",
        "## Copy into Railway's Raw Editor",
        "",
        "```",
        env_template().rstrip(),
        "```",
        "",
    ]
    return "\n".join(out)


__all__ = [
    "BY_ID",
    "CONTACT",
    "GROUPS",
    "INTEGRATIONS",
    "CheckCache",
    "Integration",
    "Result",
    "Var",
    "check",
    "env_template",
    "probe",
    "render_doc",
    "status",
]
