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

> **Status: phase 1 of 6 (the ledger).** Import, export, history and the write rules work. Impact
> previews, experiments, the chat and the `/research` page come in later phases.

## For a researcher

Phase 1 has no web page yet. On the EC2 box, ask the operator to run:

```bash
senseai-research claims --status unverified        # the values with no checked quote yet
senseai-research claims --path felidae.senses.vision
```

Each line shows a value's status, its place in the library, and the number with its unit.
`unverified` means nobody has yet found the sentence in a full-text paper that gives this number.

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

`import` is safe to run again. Values that changed upstream become new revisions; nothing is
overwritten.

## Development

```bash
cd packages/senseai-research
ruff check . && ruff format --check . && mypy && pytest -q
```

Tests use no network and no API key. `SENSEAI_PROFILES_REPO` pointing at a full profiles checkout
turns on the test that imports the real library.
