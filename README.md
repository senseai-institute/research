# senseai-research

A place to check the numbers that make Panther a panther.

The species library (`profiles/library/*.yaml`) holds values such as "a cat sees light 0.74 log
units dimmer than a human". Those values set how the robot's camera and reflexes behave at
night. This repository keeps an **evidence ledger** of them: for each value, the paper, the exact
passage it came from, the conditions it was measured under, and every correction anyone has made.
It also runs a chat assistant over that ledger and runs bench experiments on real Zurich night
recordings. A biologist can see where a number came from, correct it in plain language, see what
the correction does to the robot, and sign off on a library change.

The full design is in [`docs/SPEC.md`](docs/SPEC.md).

> **Status: phase 2 of 6.** The ledger works (import, export, history, write rules). Impact
> previews work: what a library change does to Panther's profile, reflex and world prior. So do
> experiments: the change runs on real night recordings through the bench, compared with an
> unchanged baseline. The chat and the `/research` page come in later phases.

## For a researcher

Phase 1 has no web page yet. On the EC2 box, ask the operator to run:

```bash
senseai-research claims --status unverified        # the values with no checked quote yet
senseai-research claims --path felidae.senses.vision
```

Each line shows a value's status, its place in the library, and the number with its unit.
`unverified` means nobody has yet found the sentence in a full-text paper that gives this number.

To see what a correction would do before anyone runs anything:

```bash
senseai-research impact --path felidae.senses.vision.detection_threshold --value 0
```

It lists the profile values that read this number. Each change gets one plain sentence, for
example "the frame head trusts the camera image at half confidence down to 0.316 lux (was
0.0575 lux)". A table follows with every changed point, and it says whether the world model reads
the value at all. Every number comes from re-deriving the profiles in code, not from an estimate.

To see what it does on recordings (minutes; the baseline is run once and then reused):

```bash
senseai-research experiment --path felidae.senses.vision.detection_threshold --value 0 \
    --sequences zurich_city_03_a,zurich_city_09_b --arms panther_prior,null_profile
```

The table shows each arm's hit rate before and after, with the **chance hit rate** beside it:
how often the arm would "hit" by luck alone. A hit rate is meaningful only well above its chance
rate. With fewer than 20 labelled objects the table says "too few objects to judge" instead of
a percentage.

## For an operator

```bash
uv venv && uv pip install -e "packages/senseai-research[dev]"   # needs read access to profiles
export SENSEAI_PROFILES_REPO=~/senseai/profiles                 # a profiles checkout
export RESEARCH_DATA=~/senseai/research                        # the ledger lives here

senseai-research import                  # library values -> claims; requests -> hypotheses
senseai-research export --out export/    # reviewable YAML, stable byte for byte
senseai-research backup [--s3 s3://bucket/prefix]
senseai-research schema --out schema/    # JSON Schema of every record
```

Experiments need the stack installed the way the viewer's `deploy/setup_ec2.sh` does it
(`~/senseai/.venv`), plus `SENSEAI_DATA_ROOT` (prepared DSEC recordings),
`SENSEAI_BENCH_CMD` and `SENSEAI_BENCH_PYTHON`. Each run happens in a temporary copy of the
profiles checkout, so the checkout itself is never written.

`import` is safe to run again. Values that changed upstream become new revisions; nothing is
overwritten.

## Development

```bash
cd packages/senseai-research
ruff check . && ruff format --check . && mypy && pytest -q
```

Tests use no network and no API key. `SENSEAI_PROFILES_REPO` pointing at a full profiles checkout
turns on the test that imports the real library.
