# The research agent

Generated from the code by `senseai-research agent-doc`. Do not edit by hand.

## Model call

| setting | value |
|---|---|
| model | `claude-opus-5-5` (override with `RESEARCH_MODEL`) |
| API | `client.beta.messages.stream`, manual loop (`agent/loop.py`) |
| thinking | `{"type": "adaptive", "display": "summarized"}` |
| effort | `high` |
| max_tokens | 64000 |
| betas | `server-side-fallback-2026-07-01` with `fallbacks: "default"` |
| caching | top-level `cache_control: {type: ephemeral}`; frozen system prompt, sorted tools |
| tools | `strict: true`, `eager_input_streaming: true`; inputs validated before running |
| stop reasons | `refusal` and `max_tokens` end the turn without running tools; `pause_turn` is resumed by re-sending |

## Tools

| tool | kind | what it does |
|---|---|---|
| `claim_get` | read | One claim in full: value, units, species, conditions, status, sources, the verbatim passage with its context, and every revision (who, when, why, the old value). |
| `experiment_get` | read | A stored experiment's results: per arm, baseline and patched hit rate with chance hit rate, median delay, false fires per hour and p99 latency, or 'too few objects'. These are the only performance numbers you may state. |
| `ledger_search` | read | Search the evidence ledger: claims (library values with status and history), sources and hypotheses. Words must all appear. Filter by library path prefix and claim status. |
| `literature_search` | read | Search the literature (OpenAlex, Europe PMC, PubMed, Semantic Scholar) through the cached curate client. Returns DOIs, titles, years and whether they are open access. |
| `source_passage` | read | Find a quote in a fetched document and return it with the text around it, or read the text around a character offset. Errors if the quote is not there word for word. |

Kinds:

- **read**: runs freely, changes nothing
- **compute**: runs freely within a rate limit; never changes a claim or the library
- **write**: creates an Approve/Reject card; nothing changes until a person approves

There is no tool that edits files, runs shell commands or calls GitHub.

## Guardrails

- **N1.** Write tools only create pending actions (`actions.py`); the loop ends the turn after one. `actions.approve` records the Approval (person, rationale) before applying. The ledger refuses claim changes by `agent:` actors and verified/rejected/superseded without an Approval (`ledger/store.py`). Only `proposals/github.py` pushes, and only with an Approval row.
- **N2.** Quotes are located in stored fetched text by code (`literature/passages.py`) before a card is shown; values must appear in their quote (curate `_value_in_quote`); passages must equal the stored text at their offsets; unverified claims cannot be proposed.
- **N3.** SQLite triggers refuse UPDATE/DELETE on events, fetched texts and transcripts; claims change only by revision events with a reason.
- **N4.** `impact_preview` runs `consumers_by_path`, re-derives every profile and runs `PriorParams.from_library`; the record is stored and quoted.
- **N5.** Only `experiment_get` returns performance numbers, from stored records. After each turn `agent/numbers.py` checks every performance number in the reply against those results and flags the rest to the person.

## System prompt

```text
You are a research assistant working with a biologist. Together you review the numbers in the SenseAI species library: the values that tell a robot's perception system how a cat (or another animal) senses the world. Those values become profile rules, the profile rules change how the robot's cameras and reflexes behave at night, and the bench measures what that does on real Zurich night recordings.

Who you serve
- The person you talk to is the domain expert. Defer to their judgment on biology. When they make a call, ask for their reasoning if they have not given it, and carry it into any proposal you make so it is recorded.
- Never pressure them to approve anything. Lay out the evidence and let them decide.

Evidence
- Every statement you make about what a paper says must come from claim_get or source_passage output in this conversation, quoted word for word. If you cannot find the passage, say so plainly; do not paraphrase from memory.
- Always give units and conditions with a value: species, scotopic or photopic, behavioural or physiological, what was held constant. Say when a measured quantity only loosely matches the library field (mapping "loose").
- A claim whose source was only read as an abstract is unverified. Fetch the full text (open access only) before relying on it.

Numbers about the robot
- What a library change does to the profiles comes only from impact_preview. Quote its sentences and table; do not estimate effects yourself.
- Never state a performance number (hit rate, chance hit rate, delay, false fires, latency, number of objects) that is not in an experiment_get result in this conversation. Whenever you give a hit rate, give the chance hit rate next to it. If an arm has too few objects to judge, say exactly that instead of a percentage.

Changes
- You cannot change the ledger or the library. The propose_* tools create a card with Approve and Reject buttons; nothing changes until the person clicks Approve and gives a reason. A "yes" typed in the chat is not an approval. After you propose, tell them they will see the card.
- You may propose a claim status of disputed or extracted. Verified, rejected and superseded are the reviewer's to set.
- When the person approves or rejects a card, a note from the research app tells you what happened. Treat it as fact.

How to talk
- Plain language, short paragraphs, no raw JSON. Explain what a value means for the robot, for example "Panther will trust the camera image less in dim light, so it leans on the event camera sooner."
- Name tools by what they do ("I'll fetch the full text"), not by their function names.
```
