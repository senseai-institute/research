# Deploying senseai-research next to Studio (Railway)

senseai-research is the backend and the source of truth for the ledger, the assistant, impact
previews, experiments and library proposals. Studio (`senseai-institute/studio`) is the
front end and the source of truth for the screens. Studio calls this service through its own
proxy, `/api/studio/research/*` → `RESEARCH_URL/api/research/*`, adding the signed-in name
(`X-SenseAI-User`) and a shared secret (`X-SenseAI-Proxy-Token`). Every route and every chat
event is listed in [`API.md`](API.md); the schemas are at `/api/research/openapi.json`.

```
browser ──password──▶ Studio (public) ──X-SenseAI-Proxy-Token──▶ research (private network)
                                                                    │  /data volume: ledger,
                                                                    │  caches, profiles checkout
                                                                    ▼
                                         GitHub (profiles), Anthropic, OpenAlex, …
```

## 1. The research service

1. **New service from this repository.** Railway builds the `Dockerfile`; `railway.json`
   sets the health check to `/healthz`.
2. **Add a volume** mounted at `/data`. The ledger (SQLite), the download cache and the
   profiles checkout live there, so they survive redeploys.
3. **Variables** (service → *Variables*). Required:

   | Variable | Value |
   |---|---|
   | `RESEARCH_PROXY_TOKEN` | A long random string (`openssl rand -hex 32`). The same value goes on Studio. Without it the service refuses to start. |
   | `GITHUB_TOKEN` | A fine-grained token on `senseai-institute/profiles`: Contents read (to clone it at start) and, for library pull requests, Contents and Pull requests read and write. |
   | `ANTHROPIC_API_KEY` | For the assistant. |
   | `PORT` | `8100`, so Studio knows where to call. |

   Then the keys in [`APIS.md`](APIS.md) (OpenAlex, Semantic Scholar, NCBI, contact email,
   …). The end of that file has a block for *Raw Editor*. Optional: `SENSEAI_PROFILES_BRANCH`
   (default `main`), `SENSEAI_PROFILES_GIT` (default the GitHub repository).
4. **No public domain.** Leave *Networking → Public* off; Studio reaches the service over
   Railway's private network.

On start, `scripts/container-start.sh` clones (later: updates) profiles into
`/data/profiles` using `GITHUB_TOKEN` in a request header (never written to disk), installs
senseai-profiles from it (it is private, so it is not in the image), imports the library into
the ledger (no model call), and serves on every address of `$PORT`.

## 2. Studio

On the Studio service, set:

| Variable | Value |
|---|---|
| `RESEARCH_URL` | `http://${{research.RAILWAY_PRIVATE_DOMAIN}}:8100` (use the research service's name in place of `research`) |
| `RESEARCH_PROXY_TOKEN` | `${{research.RESEARCH_PROXY_TOKEN}}` |
| `VIEWER_PASSWORD` | The password people sign in with. |

Studio's Research tab then shows the chat, the values that need checking, and *Settings*,
where each API key's status can be checked and tested.

## What does not run on Railway yet

- **Experiments** (`experiment_run`) need senseai-bench, world and the DSEC recordings, which
  are not in this image. An experiment started there is stored as failed with the reason
  "is senseai-bench installed?". Experiments run on the EC2 box
  (`scripts/install_research_ec2.sh`).
- **World prior** rows in impact previews say "not checked" without senseai-world.

## Running it beside Studio on one machine

Without `RESEARCH_PROXY_TOKEN` the service listens on 127.0.0.1 only and trusts callers on
that machine: `senseai-research serve`, and `RESEARCH_URL=http://127.0.0.1:8100` on Studio.
