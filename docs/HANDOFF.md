# From evidence to circuits: quality, estimates, and the handoff package

Research finds and checks the evidence. Circuits builds the model. This document covers what
happens in between: how research scores each piece of evidence, how it turns several claims
into one value for a circuit setting (a *knob*), and what package it hands to
[senseai-circuits](https://github.com/senseai-institute/circuits). Research's work ends when a
person releases that package.

```
claims in the ledger ──► quality per claim ──► estimate per knob ──► package ──► (person approves) ──► circuits
   (sources, quotes)       (score + reasons)     (value, interval,      (JSON,                         CircuitParams
                                                   readiness)             digest)                        .from_bundle_meta
```

Code: `packages/senseai-research/src/senseai_research/handoff/`. JSON Schema of the package:
`schema/handoff.schema.json` (also `GET /api/research/schema/handoff`).

## 1. Quality: how far one claim can be trusted as a model input

Each claim gets a score from 0 to 1. The score is a **product of factors**, and each factor
comes with one plain sentence, so a reader can see what lowered a claim's score and by how much
(`handoff/quality.py`).

| Factor | Values |
|---|---|
| status | verified 1.0 · extracted 0.8 · unverified 0.55 · disputed 0.45 |
| reading | passage stored verbatim 1.0 · full text available or read 0.95 · abstract only 0.85 |
| mapping | exact 1.0 · close 0.9 · loose 0.65 |
| method | review 0.75 · model 0.65 · a method unusual for this knob 0.85 · not recorded 0.95 |
| species | not a representative species of the clade 0.85 · not recorded 0.85 |
| library confidence | medium 0.9 · low 0.7 |
| vetting | 0.8 for each flag (predatory venue, PubPeer, …) and for a Crossref mismatch |

**Hard gates** remove a claim outright, whatever its other factors:

- a person rejected or superseded it;
- its source was retracted;
- it cites no known source;
- its value cannot be converted to the knob's unit.

Labels: **high** ≥ 0.70, **medium** ≥ 0.45, otherwise **low**.

These numbers are a first proposal written down so they can be argued with; they are not a fitted
model. `QUALITY_VERSION` goes into every package, so a change to the table shows up there.
`GET /api/research/claims/{id}` now includes the claim's `quality` and the knob it sets.

## 2. Rules: which measurement sets which knob

The knob list comes from circuits' registry. `handoff/circuits_knobs.json` is a copy of
`senseai-circuits knobs --json` taken at circuits `f465ce1`, and a test checks the copy against the
installed package.

Knobs fall into three groups:

- **physics** (11 knobs) are set by the circuit design. Literature never sets them, and they are
  listed in the package as `engineering_knobs`.
- **measured and proposed knobs that a published quantity can set** each have one rule
  (`handoff/rules.py`):

| Knob | Library leaf (below the clade) | Unit | Transform |
|---|---|---|---|
| `ts.sigma_bearing_deg` | `senses.hearing.spatial_resolution` | deg | σ = minimum audible angle (the leaf senseai-world already reads) |
| `cs.sigma_center_deg` | `senses.vision.rgc_center_radius` | deg | σ = r / √2 |
| `cs.surround_ratio` | `senses.vision.rgc_surround_ratio` | ratio | as is |
| `cs.adapt_tau_ms` | `senses.vision.light_adaptation_tau` | ms | as is |
| `emd.delay_tau_ms` | `senses.vision.motion_tf_optimum` | Hz | τ = 1000 / (2π f) ms (Reichardt optimum, Borst & Egelhaaf 1989) |
| `lgmd.ttc_alpha` | `senses.vision.looming_peak_angle` | deg | α = 2 / θ_peak (Gabbiani et al. 1999) |

- **knobs that are not published quantities** (model weights, thresholds, sizes in grid cells or
  ticks) have a written reason. Their gap says to fit them in an experiment.

A test fails if a non-physics knob has neither a rule nor a reason.

Only `senses.hearing.spatial_resolution` exists in the library today. The other leaves are
**asks**: each gap tells a contributor exactly what to measure and where to put it. Their names
follow the circuits design table and may change when the P0 species-file document lands in
profiles.

## 3. Estimates: from claims to one value per knob

For each rule, every claim at `<clade>.<leaf>` is normalised to the rule's unit and run through
the transform (`handoff/estimate.py`). The estimate is then built from the claims that pass the
gates:

- **value**: the mean of the claims' central values, weighted by quality score;
- **interval**: from the lowest to the highest end of the claims' own ranges, in knob units (a
  model builder can use it as the prior's width);
- **agreement**: claims disagree when the spread of their central values is more than 25% of
  the value;
- **readiness**:
  - `ready`: two or more independent sources agree, the best claim scores high, and the value is
    in the knob's range;
  - `provisional`: some usable evidence, but less than that;
  - `insufficient`: no usable claim, or a value outside the knob's range;
- **strength** (0–1), one number for weighting a prior: the best score × 0.7 for one source, 0.9
  for two, 1.0 for three or more, × 0.5 when the claims disagree.

## 4. The package

`GET /api/research/handoff/{clade}` returns the current draft, and `senseai-research handoff
<clade>` prints it in plain words (`--json` or `--out FILE` for the file). Top-level fields:

| Field | |
|---|---|
| `format` | `senseai-research/handoff@1` |
| `circuits` | `{knob: value}` for the knobs whose readiness meets the policy. **This is the circuits contract**: the whole package is a valid bundle `meta`, so `CircuitParams.from_bundle_meta(package)` reads it, and the same mapping is a profile spec's `circuits:` section. |
| `policy` | `min_readiness`: `provisional` (default) or `ready` |
| `knobs` | for every literature knob: its rule, estimate, interval, readiness, strength, the evidence used and excluded (each with its quality factors), and whether it was passed on |
| `gaps` | `no_evidence`, `no_literature`, `out_of_range`, `below_policy`, `disagree`, each with a sentence and an ask |
| `engineering_knobs` | physics knobs, which circuits sets by design |
| `sources` | the papers behind every value passed on |
| `provenance` | research version and SHA, profiles SHA, the circuits registry SHA, the quality version |
| `digest` | sha256 of everything else (canonical JSON) |

A package depends only on the ledger, the profiles checkout and the code: the same inputs give
the same bytes. It contains no clock time.

## 5. Release: a person approves, then circuits receives it

Releasing is approval-gated like every other write (N1):

1. `POST /api/research/handoff/{clade}/propose` (or `senseai-research handoff <clade> --propose`)
   creates a pending action. Its card lists each knob value that would change.
2. A person approves it at `/api/research/actions/{id}/approve` with a rationale. Research then
   rebuilds the package. If the evidence changed since the proposal, the digest differs and
   nothing is sent: the release is proposed again, so people only approve what will actually
   go out.
3. The package is written to `RESEARCH_DATA/outbox/handoff/<clade>.<digest>.json` and served at
   `GET /api/research/handoff/{clade}/released`.
4. If `RESEARCH_CIRCUITS_REPO` is set (for example `senseai-institute/circuits`), research also
   pushes the package as `research/<clade>.handoff.json` on branch
   `research/handoff-<clade>-<digest>` and opens a pull request. It never pushes to the default
   branch. The token is `RESEARCH_CIRCUITS_TOKEN`, or `GITHUB_TOKEN` if that is not set, with
   Contents and Pull requests (read and write) on that repository. If the PR fails, the release
   still happens and the error is recorded.
5. A `HandoffRelease` record (who, why, digest, file, PR) is added to the ledger. It is never
   changed afterwards.

On the circuits side:

```python
import json
from senseai_circuits.params import CircuitParams

pkg = json.load(open("research/felidae.handoff.json"))
params = CircuitParams.from_bundle_meta(pkg)   # unknown or out-of-range values raise KnobError
```

`handoff.verify(doc)` (research) checks the format and the digest.

## What the library gives today

Of 12 literature knobs, felidae gets one value: `ts.sigma_bearing_deg` = 5°, provisional, strength
0.29. It rests on one unverified claim from one source, and only the abstract is known to have
been read. Strigidae (owls) gets 2°. Every other knob keeps circuits' default, and the gaps list
the measurements that would set them. Moving a value to `ready` takes a verified quote and a
second independent source.
