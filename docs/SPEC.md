# senseai-research: spec

Status: draft v1, 2026-10-17. Written from an audit of the current mains: profiles 018d2d6, world 8ec2cfd, bench d7c963a, viewer 96d83e8.

## 1. Why this exists

The species library (`profiles/library/*.yaml`) is what makes Panther a panther. Library values become profile rules, profile rules become tokenizer gains and the reflex threshold, and those change what the robot does at night. Today those values come from an LLM pipeline (`senseai-library`, in `profiles/.../curate/`). It searches the literature, pulls quotes and drafts YAML. That extraction is error-prone: it picks the wrong species, the wrong condition, the wrong table row, or units that look right but aren't.

The person who can catch those errors is a biologist (PhD), not a programmer. They need to:

1. **See** where a number came from: the paper, the passage, the conditions, and who extracted it (the pipeline or a person).
2. **Question and correct it** in plain language ("that's the optics-included value; with the pupil factored out cats and humans are similar").
3. **See what the correction does**: which profile values change, which world-prior parameters change, and whether Panther gets better or worse on real Zurich night recordings.
4. **Sign off**, so the change lands in the library as a reviewed PR, with their rationale attached.

The goal is an agentic chat (Claude with tools) over an evidence ledger, with the bench as the experiment engine.

**Out of scope for v1:**

- Overfitting guardrails, held-out splits and multiple-comparison corrections. The owner said: "Don't worry about overfitting for now. We will add more data." Every experiment still reports the chance hit rate (the luck check), because it is cheap and already in the bench.
- Training learned world models.
- Multi-user editing conflicts. Assume one or two researchers.

**Non-negotiables:**

- **N1. The agent never writes the library or any other repo directly.** It may only produce proposals. A proposal becomes a PR only after a human clicks Approve in the UI. A chat message saying "yes" does not count. The approval records who approved and their rationale.
- **N2. Every claim carries a verbatim quote** from a source the system has actually fetched, plus a locator (section, page, table or figure). The existing check in `curate/extract.py` (`check_claim`, `_value_in_quote`) is the floor. A claim with no verifiable quote can exist only as `status: unverified`, and it can never be proposed to the library.
- **N3. Nothing is overwritten.** Corrections are new revisions with a reason. The full history of a claim (who, when, why, previous value) is always visible.
- **N4. Impact comes from code, not from the agent's guess.** "Which profile values read this?" is answered by `senseai_profiles.research.consumers_by_path` and by actually re-deriving the profile. The agent narrates; the code computes.
- **N5. Experiment numbers come from bench output files only.** The agent may never type a result number that is not in a stored experiment record.

## 2. Decisions made (owner can flip)

| # | Decision | Chosen | Why |
|---|---|---|---|
| D1 | Where the chat UI lives | A **viewer page** (`/research`). Its backend is a separate service from this repo, proxied by the viewer server. | One login, one design system, one URL on the EC2 box. The viewer already does auth (`auth.py`, user `senseai`) and SSE. |
| D2 | Curate pipeline location | **Phase 1: import** `senseai_profiles.curate` (dependency on `senseai-profiles[curate]`). **Phase 5: move** it into this repo, leaving a re-export shim in profiles. | Moving first would break `senseai-library` users on day one. Wrapping first gets the chat working sooner. The move is mechanical once the tests run here. |
| D3 | Ledger storage | **SQLite (WAL)** under `$RESEARCH_DATA`, with an append-only `events` table and projected current-state tables. `senseai-research export` writes reviewable YAML. | Append-only gives N3 for free. One file, easy to back up to S3 (reuse `curate/cache.py sync_s3`). |
| D4 | Model | `claude-opus-5-5`, adaptive thinking, effort `high`, streaming, server-side `fallbacks: "default"` | Correctness on scientific text matters more than cost. Fallbacks avoid a silent dead-end on a refusal. |
| D5 | Agent loop | A **manual loop** on the Anthropic Python SDK (`client.beta.messages.stream`), not the tool runner | We need to stream to SSE, stop before write tools (approval), persist every turn append-only, and handle `pause_turn`. The Python tool runner cannot resume `pause_turn`. |
| D6 | PRs to profiles | `git` worktree of profiles, branch `research/<proposal-id>`, push with a fine-grained token (`GITHUB_TOKEN`, contents and PR scope on `senseai-institute/profiles` only), then GitHub REST `POST /repos/{o}/{r}/pulls` via `httpx` | No `gh` dependency. The token is the only write credential and is scoped to one repo. |

## 3. Repository layout

```
research/
  README.md                       quickstart for a researcher + for an operator
  docs/SPEC.md                    this file
  docs/AGENT.md                   tool list, system prompt, guardrails (generated from code: `senseai-research agent-doc`)
  packages/senseai-research/
    pyproject.toml                deps: senseai-profiles[curate], senseai-world, senseai-bench, anthropic, fastapi, uvicorn, sse-starlette, httpx, pydantic>=2, jsonschema, pyyaml
    src/senseai_research/
      __init__.py
      config.py                   Settings (env, see §9)
      ledger/
        models.py                 pydantic: Source, Passage, Claim, ClaimRevision, Hypothesis, Experiment, Proposal, Approval, Event
        store.py                  SQLite: append_event(), projections, queries; schema migrations
        importers.py              import existing library values + CITATIONS.md + curate run state as claims
        export.py                 YAML export of the ledger (for review / git)
      literature/
        __init__.py               thin facade over senseai_profiles.curate (search, fetch, extract, vet, units, cache)
        passages.py               locate a quote in fetched text; return surrounding context with char offsets
      impact/
        consumers.py              library path -> profile fields (wraps research.consumers_by_path)
        derive.py                 re-derive profile values with an overridden library; diff old vs new
        world.py                  PriorParams.from_library on old vs new library; diff, with measured/default flags
      experiments/
        workspace.py              temp worktree of profiles with a patched library file
        bench.py                  run `senseai-bench replay-compare` (subprocess), parse report_json + events.jsonl
        record.py                 Experiment record: inputs (library diff hash, sequences, arms, git SHAs), outputs (per-arm scores incl. chance_hit_rate)
      proposals/
        diff.py                   build a YAML patch to profiles/library/<clade>.yaml from approved claims (ruamel round-trip, keeps comments)
        github.py                 branch, commit, push, open PR (body template §7.4)
      agent/
        tools.py                  tool definitions (JSON schema, strict) + handlers
        prompts.py                system prompt (frozen; cache-friendly)
        loop.py                   manual streaming loop, approval stop, persistence, pause_turn, refusal handling
        transcript.py             append-only conversation store (in the ledger DB)
      server/
        app.py                    FastAPI: /api/research/* (see §8)
        sse.py
      cli.py                      senseai-research {serve, import, export, ask, experiment, agent-doc, backup}
    tests/
      fixtures/                   tiny library, fake fetched paper text, canned bench report_json, recorded Anthropic responses
      ...
  .github/workflows/ci.yml        ruff, mypy, pytest (no network, no API key)
```

## 4. Data model (pydantic v2; JSON Schema exported to `schema/`)

All records have `id` (stable slug or ULID), `created_at`, `created_by` (`user:<name>` | `pipeline:<step>@<version>` | `agent:<conversation-id>`).

**Source**: `doi | url`, `title`, `authors`, `year`, `venue`, `oa_status`, `fulltext_available: bool`, `fetched_text_sha256`, `retrieved_at`, `vetting` (from `curate/vet.py`: retraction, crossref match).

**Passage**: `source_id`, `char_start`, `char_end`, `text` (verbatim), `section`, `page | table | figure` (nullable), `context_before/after` (≤ 600 chars each). Only created by code that found the text in the fetched document.

**Claim** is the core record:
```
id, library_path            e.g. felidae.senses.vision.detection_threshold
quantity                    {value | low+high, unit, canonical_value, canonical_unit}   (curate/units.py)
species, conditions, method (behavioural | electrophysiology | anatomical | model | review)
passage_id                  REQUIRED for status >= extracted
mapping                     exact | close | loose      (does the measured thing equal the library field?)
status                      unverified | extracted | disputed | verified | rejected | superseded
provenance                  pipeline (curate run id) | user | agent-proposed
revision                    int; every change is a ClaimRevision event with {field, old, new, reason, by}
notes_by_reviewer           free text, the biologist's reasoning
```
The status transitions `→ verified`, `→ rejected` and `→ superseded` require a human Approval (N1). Agent-originated edits stop at `disputed` or `extracted` with `provenance: agent-proposed`.

**Hypothesis**: `statement`, `origin` (library open_question id | research request id (`profiles/research/requests/*.yaml`) | experiment id | user), `linked_claims[]`, `prediction` (what bench result would support/refute it, stated *before* running), `status` (open | supported | refuted | inconclusive), `experiments[]`.

**Experiment**: `hypothesis_id?`, `library_patch` (unified diff + sha), `profiles_sha`, `world_sha`, `bench_sha`, `sequences[]`, `arms[]`, `seed`, `command` (exact argv), `started/finished`, `status`, `outputs` (path to run dir, parsed `report_json`: per arm `hit_rate`, `chance_hit_rate`, `median_delay_s`, `unattributed_fires_per_h`, `latency_p99_ms`, `tracks`, `hits`), `baseline_experiment_id`.

**Proposal**: `claim_ids[]`, `library_file`, `yaml_patch`, `experiment_ids[]`, `rationale`, `status` (draft | approved | pr_open | merged | closed), `pr_url`.

**Approval**: `target` (claim revision | proposal), `decision` (approve | reject), `by` (authenticated user), `rationale` (required, ≥ 1 sentence), `at`.

**Event**: `seq`, `at`, `actor`, `type`, `payload`. This is the append-only log every other table is projected from.

**Import (Phase 1)**: every quantity in `profiles/library/*.yaml` becomes a Claim, along with the citations and notes on that quantity. Status rules:
- `extracted` if a curate run state has a matching verified quote.
- `unverified` otherwise. For example, felidae `detection_threshold` cites a paper marked `abstract` in `CITATIONS.md`, so it has no full-text quote yet.

Every open `profiles/research/requests/*.yaml` and every clade `open_questions` entry becomes a Hypothesis with `status: open`.

## 5. Impact map (N4)

Given a candidate change to library path `P` (old value `a`, new value `b`):

1. **Consumers.** `consumers_by_path(specs)` lists the profile fields that read `P`. Today `felidae.senses.vision.*` feeds:
   - `panther.frame.gain_lut` (`vision_supply_vs_demand`)
   - `panther.event.attention` (`vision_deficit_boost`)
   - the same two fields on `panther_vision`
   
   See `research/requests/felidae-vision-q-1.yaml`, the `why.consumers` block.
2. **Profile re-derivation.** Write the library with `b` into a temp copy, load the profile registry against it, and diff every derived field: gain LUT values, attention weights, reflex `commit_threshold` and the θ that follows from it (θ = 2.6 × commit_threshold / 0.5, per reflex decision D1). Show the diff as a small table: field, old, new, Δ%.
3. **World prior.** Run `PriorParams.from_library(old)` and `PriorParams.from_library(new)`, and report each param with its `measured`/`default` flag. **Audit fact:** on world 8ec2cfd, `from_library` reads only `senses[hearing].spatial_resolution` (deg) → `sigma_bearing_deg`. Every other prior parameter is a flagged default. So a vision correction today changes the *profile*, not the world prior. The UI must say so plainly ("this value does not reach the world model yet"), and that gap is itself a Hypothesis to log.
4. **Reflex.** Report θ per arm before and after.

The impact result is a stored, versioned record. The agent quotes it; it does not recompute it.

## 6. Experiments (bench is the engine)

- Run in a temp worktree of profiles at the current main SHA, with the patched library file. Set `SENSEAI_PROFILES_REPO` to that worktree.
- Command (subprocess, `cwd=run_dir`, same as viewer `runner.py`):
  ```
  $SENSEAI_BENCH_CMD replay-compare --data $SENSEAI_DATA_ROOT --sequences <a,b> \
     --arms panther_prior,panther_vision,null_profile[,torchvision] \
     --events run/events.jsonl --runlog run/runlog.h5 --out run/
  ```
  Reuse the exact argument builder from viewer `runner.py` if it is importable. Otherwise copy it, with a comment naming the source.
- A **baseline** is the same command on unpatched main, run once per (sequences, arms, SHAs) and cached by that key.
- Parse `report_json`. The headline compares patched and baseline per arm: hit rate (with chance hit rate beside it), median delay, unattributed fires per hour and p99 latency.
- Hits are counted per labelled object. If an arm has fewer than 20 labelled objects (the viewer's `MIN_OBJECTS`), the headline says "too few objects to judge", not a percentage.
- Experiments are long (minutes). Run them as background jobs with progress streamed from `events.jsonl`, the same tailer the viewer uses. At most one at a time; queue the rest.
- `panther_prior` is the default arm. It needs no checkpoint, so it shows a library change end to end without training.

## 7. The agent

### 7.1 Model call (per D4/D5)
- `client.beta.messages.stream(model="claude-opus-5-5", max_tokens=64000, thinking={"type":"adaptive","display":"summarized"}, output_config={"effort":"high"}, betas=["server-side-fallback-2026-07-01"], fallbacks="default", system=..., tools=..., messages=...)`.
  - The builder must confirm every parameter against the `claude-api` skill before writing code. Do not rely on memory.
- Use top-level auto prompt caching. Keep the system prompt and the tool list byte-stable: no timestamps, sorted keys.
- Persist the assistant `response.content` unchanged, append-only. Never edit earlier turns (preserved thinking).
- Check `stop_reason` before running tools: handle `refusal`, `max_tokens` and `pause_turn` (resume by re-sending). Return tool errors as `tool_result` with `is_error: true`.
- Tools use `strict: true`. With streaming, set `eager_input_streaming: true` and validate the parsed input against the schema before executing.

### 7.2 Tools

Read tools run freely. Compute tools run freely but are rate-limited. Write tools **never execute**: they create a *pending action*, and the loop ends the turn with a card the human must approve or reject in the UI (N1).

| tool | kind | does |
|---|---|---|
| `ledger_search(query, library_path?, status?)` | read | claims, sources, hypotheses |
| `claim_get(claim_id)` | read | claim with full revision history and its passage in context |
| `source_passage(source_id, quote? , around_char?)` | read | locate text in the fetched document and return context; error if not found (N2) |
| `literature_search(query, species?, since?)` | read | OpenAlex, Semantic Scholar, PubMed, Europe PMC via curate `search.py` (cached) |
| `fetch_fulltext(doi)` | compute | curate `fetch.py`, OA only; stores Source |
| `extract_from_source(source_id, library_path)` | compute | curate `extract.py` restricted to one path; results land as `extracted` claims with `provenance: pipeline` |
| `impact_preview(library_path, new_quantity)` | compute | §5; returns the stored impact record id plus a summary |
| `experiment_run(library_patch_or_claim_ids, sequences, arms, hypothesis_id?)` | compute (job) | §6; returns a job id; progress streams in UI |
| `experiment_get(experiment_id)` | read | stored outputs only (N5) |
| `propose_claim_revision(claim_id, changes, reason)` | **write** (pending) | e.g. value, conditions, species, mapping, status → disputed |
| `propose_new_claim(library_path, quantity, source_id, quote, …)` | **write** (pending) | quote must verify against fetched text first, or the tool errors |
| `propose_hypothesis(statement, prediction, linked_claims)` | **write** (pending) | |
| `propose_library_change(claim_ids, experiment_ids, rationale)` | **write** (pending) | builds YAML patch; on approval → PR (D6) |

There is no tool that edits files, runs shell commands or calls GitHub directly.

### 7.3 System prompt (intent; final text in `agent/prompts.py`)
- **Role:** a research assistant to a biologist reviewing what a robot's animal model believes.
- **Who it serves:** the user is the domain expert. Defer to their judgment on biology, ask for their reasoning, and record it. Never pressure them to approve.
- **Evidence:** every factual statement about a paper must come from `source_passage` / `claim_get` output in this conversation. When you cannot find the passage, say so.
- **Numbers:** never state a performance number that is not in an `experiment_get` result. State the chance hit rate next to any hit rate.
- **Units and conditions:** always state them (scotopic vs photopic, species, behavioural vs physiological). Flag `mapping: loose`.
- **Tone:** plain language. Explain what a profile or reflex value means for the robot ("Panther will brighten dark frames less, so…").
- **Changes:** changes are proposals; tell the user they will see an Approve button.

### 7.4 PR body template
Title: `library(<clade>): <path> <old> → <new>`.

The body lists:
- the claims, each with DOI and verbatim quote;
- the reviewer's rationale;
- the impact table;
- the experiment table (baseline vs patched, chance hit rates, SHAs, exact command);
- "Approved by <user> at <time> in senseai-research".

The PR also runs the profiles CI.

## 8. Server API (FastAPI, mounted behind the viewer proxy)

- `POST /api/research/conversations` → id.
- `GET /api/research/conversations/{id}` → transcript.
- `POST /api/research/conversations/{id}/messages` (body: text, optional `focus: {claim_id|library_path}`) → SSE stream with these events:
  - `text_delta`
  - `thinking_summary`
  - `tool_call` (name plus a short human label)
  - `tool_result_summary`
  - `pending_action` (card payload)
  - `job_progress`
  - `done`
  - `error`
- `POST /api/research/actions/{id}/approve|reject` with `{rationale}`. Records the Approval, applies it, and posts a synthetic `user` message to the conversation saying what was approved, so the agent knows.
- `GET /api/research/claims`, `/claims/{id}`, `/hypotheses`, `/experiments/{id}`, `/impact/{id}`, `/proposals`.
- `GET /api/research/health` → model reachable, data root, bench command, profiles SHA.

**Auth:** the viewer proxy forwards the authenticated user in `X-SenseAI-User`. The research server trusts it only from `127.0.0.1` and otherwise refuses. Every Approval stores that user.

## 9. Configuration (env; `config.py`)

| var | meaning |
|---|---|
| `ANTHROPIC_API_KEY` | (or any SDK-resolved credential) |
| `RESEARCH_DATA` | default `~/senseai/research` (sqlite, caches, run dirs) |
| `RESEARCH_PORT` | default 8100, bind 127.0.0.1 |
| `SENSEAI_PROFILES_REPO` | clone used as base for worktrees |
| `SENSEAI_DATA_ROOT` | same names as the viewer runner, so `viewer.env` from `senseai_poc_ec2.sh` can be sourced as-is |
| `SENSEAI_BENCH_CMD` | same as above |
| `SENSEAI_BENCH_PYTHON` | same as above |
| `GITHUB_TOKEN` | optional; without it, approval produces a patch file + PR body instead of a PR |
| `RESEARCH_CURATE_S3` | optional cache mirror |

## 10. Viewer page (second PR, in `viewer`)

`web/src/pages/research.ts` plus a server proxy route `/api/research/* → http://127.0.0.1:$RESEARCH_PORT`. The proxy keeps auth and adds `X-SenseAI-User`. If `RESEARCH_URL` is unset, the nav item is hidden.

Layout: chat on the left (60%), context panel on the right.

**Context panel tabs:**
1. **Claim:** value, units, species, conditions, status badge, revision history.
2. **Source:** passage with the quote highlighted, plus context and the DOI link.
3. **Impact:** consumer list, profile diff table, world-prior row with "does not reach the world model yet" when true.
4. **Experiment:** baseline vs patched per arm, chance hit rate beside each hit rate, a link to open the run in the existing Compare/video page (`runlog.h5`).

**Chat elements:**
- Pending-action cards are inline in the chat, with Approve/Reject buttons and a required rationale box.
- Tool calls show as compact chips ("Searching PubMed for cat scotopic contrast sensitivity…"). They are expandable but never raw JSON by default.
- Start screen: "Open questions" (hypotheses sorted by priority, from the research requests' `why.priority`) and "Least-verified values Panther depends on".

Reuse the existing design tokens and components. Write a web smoke test with a fake backend.

## 11. First end-to-end scenario (acceptance test, scripted in `tests/e2e/`)

1. The user opens `/research`, clicks "Least-verified values". `felidae.senses.vision.detection_threshold = −0.74 log10 re human` is listed as `unverified` because its source is abstract-only.
2. The user asks: "Where does −0.74 come from, and is it the right number for a robot camera?" The agent calls `claim_get` and `fetch_fulltext(10.1152/jn.90641.2008)` (PMC2724355 is OA), then shows the passage. It notes the library's own caveat: most of the advantage is optics, and with optics factored out cat and human scotopic sensitivity are similar.
3. The user says: "For a camera, the optics are the lens, not the cat's pupil. Use the optics-removed value." The agent calls `propose_claim_revision`, and a card appears. The user approves with a rationale, and the claim gets a new revision. The old one stays visible.
4. The agent calls `impact_preview`. The table shows `panther.frame.gain_lut` and `panther.event.attention` (and the `panther_vision` equivalents) changing, and the world-prior row "unchanged: not read by world yet".
5. The agent calls `experiment_run` on two night sequences with `panther_prior` and `null_profile`, plus the cached baseline. Progress streams. The result table shows hit rate with chance hit rate, median delay and false fires per hour, all from the stored record.
6. The user asks for a library change. A `propose_library_change` card appears; the user approves. With `GITHUB_TOKEN` set, a PR opens on profiles with the body from §7.4. Without it, the patch and the PR body are offered as downloads.

In CI this runs with:
- recorded Anthropic responses (fixture replay, no key);
- a fixture paper text;
- a stub bench that writes a canned `report_json`/`events.jsonl`;
- a fake GitHub server.

## 12. Phases and acceptance

| phase | delivers | done when |
|---|---|---|
| P1 Ledger | models, SQLite store, importer from profiles library/research requests, export, CLI `import/export` | importing current profiles main yields one claim per library quantity, hypotheses for each open request, round-trip export is stable, tests green |
| P2 Impact + experiments | `impact/*`, `experiments/*`, CLI `senseai-research experiment --path P --value V --sequences …` | on synthetic DSEC (bench's own test fixtures or `make_synthetic_dsec.py`), changing the felidae vision threshold produces a non-empty profile diff and an experiment record with chance_hit_rate per arm |
| P3 Agent | tools, loop, transcript, approvals, CLI `senseai-research ask` (terminal chat) | scenario §11 passes headless with recorded responses; live smoke with a real key passes manually; write tools provably never execute without an Approval row (test) |
| P4 Server + viewer page | FastAPI + SSE, viewer PR (page + proxy) | scenario §11 passes in Playwright against the viewer with fake backends; screenshot set in `docs/screens/` |
| P5 Curate move | move `curate/` + its tests here, shim in profiles re-exporting with `DeprecationWarning`, `senseai-library` entry point moves | profiles and research CIs green; `senseai-library --help` works from either install |
| P6 EC2 | `scripts/install_research_ec2.sh` that reuses `~/senseai/.venv` and `viewer.env`, adds `research-ctl.sh {start,stop,status,logs}` | on the EC2 box from `senseai_poc_ec2.sh`, `/research` loads behind the viewer password |

## 13. Testing rules
- No network and no API key in CI. Anthropic calls go through one client factory that tests replace with a recorded-response fake. HTTP to literature APIs goes through curate's `CachedClient` with fixture cache.
- Property test: no code path writes `profiles/` except `proposals/github.py`, and that one only runs after an Approval row exists for the proposal.
- Golden test: the impact diff for the felidae threshold change against a pinned fixture library.
- `ruff`, `mypy --strict` on `src/`, `pytest -q`. Python ≥ 3.10.

