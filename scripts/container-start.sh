#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
#
# Start senseai-research in a container (Railway). Steps, each checked before it acts:
#   1. refuse to start without RESEARCH_PROXY_TOKEN (the server listens on a public address);
#   2. get the profiles checkout: SENSEAI_PROFILES_REPO if it is a checkout already, else clone
#      (or update) $SENSEAI_PROFILES_GIT into $RESEARCH_DATA/profiles with GITHUB_TOKEN. The
#      token goes in a request header for that one command and is never written to disk;
#   3. install senseai-profiles from that checkout (it is private, so not in the image);
#   4. import the library into the ledger (safe to repeat; no model is called);
#   5. serve on every address: [::] (IPv4 and IPv6; Railway's private network can be IPv6)
#      when the machine has IPv6, else 0.0.0.0. RESEARCH_HOST overrides.
# Lines starting with "==>" are progress.

set -euo pipefail

step() { echo "==> $*"; }
fail() {
  echo "==> cannot start: $*" >&2
  exit 1
}

DATA="${RESEARCH_DATA:-/data}"
PORT="${PORT:-${RESEARCH_PORT:-8100}}"
REPO="${SENSEAI_PROFILES_GIT:-https://github.com/senseai-institute/profiles}"
BRANCH="${SENSEAI_PROFILES_BRANCH:-main}"

[[ -n "${RESEARCH_PROXY_TOKEN:-}" ]] ||
  fail "set RESEARCH_PROXY_TOKEN (the same random string on this service and on Studio)"
mkdir -p "$DATA"
[[ -w "$DATA" ]] || fail "$DATA is not writable; mount a volume there (Railway: Volumes)"

git_auth() {
  if [[ -n "${GITHUB_TOKEN:-}" ]]; then
    local basic
    basic="$(printf 'x-access-token:%s' "$GITHUB_TOKEN" | base64 | tr -d '\n')"
    git -c "http.https://github.com/.extraheader=Authorization: Basic $basic" "$@"
  else
    git "$@"
  fi
}

PROFILES="${SENSEAI_PROFILES_REPO:-$DATA/profiles}"
if [[ -d "$PROFILES/.git" ]]; then
  step "updating the profiles checkout in $PROFILES ($BRANCH)"
  git_auth -C "$PROFILES" fetch --quiet --depth 50 origin "$BRANCH" ||
    echo "    could not fetch; using the checkout as it is"
  git -C "$PROFILES" checkout --quiet -B "$BRANCH" FETCH_HEAD 2>/dev/null || true
elif [[ -d "$PROFILES/library" ]]; then
  step "using the profiles folder at $PROFILES"
else
  step "cloning $REPO ($BRANCH) into $PROFILES"
  git_auth clone --quiet --depth 50 --branch "$BRANCH" "$REPO" "$PROFILES" ||
    fail "could not clone $REPO. It is private: set GITHUB_TOKEN to a token that can read it."
fi
export SENSEAI_PROFILES_REPO="$PROFILES"

step "installing senseai-profiles from the checkout"
pip install --quiet --no-cache-dir --no-deps -e "$PROFILES/packages/senseai-profiles" ||
  fail "could not install senseai-profiles from $PROFILES/packages/senseai-profiles"

step "importing the library into the ledger (no model is called)"
senseai-research import

HOST="${RESEARCH_HOST:-}"
if [[ -z "$HOST" ]]; then
  if [[ -s /proc/net/if_inet6 ]]; then HOST="::"; else HOST="0.0.0.0"; fi
fi
step "serving on $HOST port $PORT"
exec senseai-research serve --host "$HOST" --port "$PORT"
