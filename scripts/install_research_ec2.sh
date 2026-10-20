#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
#
# Install or repair senseai-research on the EC2 box the SenseAI viewer runs on. Safe to run
# again: every step checks before it acts. No language model is called during install.
#
#   git clone https://github.com/senseai-institute/research ~/senseai/research-repo
#   bash ~/senseai/research-repo/scripts/install_research_ec2.sh
#   research-ctl.sh start
#
# What it reuses (from the viewer's deploy/setup_ec2.sh):
#   ~/senseai/.venv                    the shared Python environment with the SenseAI stack
#   ~/senseai/profiles                 the profiles checkout (read only; PRs go through GitHub)
#   ~/senseai/viewer.env, else /etc/senseai-viewer.env
#                                      the viewer's settings (SENSEAI_DATA_ROOT, bench, ...);
#                                      RESEARCH_URL is added there so the viewer shows /research
#
# What it writes:
#   ~/senseai/research.env             research settings (port, data folder, profiles checkout)
#   ~/senseai/research/                the ledger, caches and experiment runs (RESEARCH_DATA)
#   ~/.local/bin/research-ctl.sh       start | stop | status | logs
#
# Environment (all optional):
#   SENSEAI_STACK_DIR    the stack folder                  (default ~/senseai)
#   RESEARCH_PORT        the research server's port        (default 8100, 127.0.0.1 only)
#   RESEARCH_DATA        the ledger folder                 (default $SENSEAI_STACK_DIR/research)
#   RESEARCH_SKIP_PIP=1  do not install Python packages (tests)
#
# Lines starting with "==>" are progress.

set -euo pipefail

main() {
  local STACK="${SENSEAI_STACK_DIR:-$HOME/senseai}"
  local VENV="$STACK/.venv"
  local PY="$VENV/bin/python"
  local PORT="${RESEARCH_PORT:-8100}"
  local DATA="${RESEARCH_DATA:-$STACK/research}"
  local HERE REPO PKG
  HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  REPO="$(cd "$HERE/.." && pwd)"
  PKG="$REPO/packages/senseai-research"
  export PATH="$HOME/.local/bin:$PATH"

  step "checking the SenseAI stack in $STACK"
  [[ -x "$PY" ]] || fail "no Python environment at $VENV. Run the viewer's deploy/setup_ec2.sh first."
  [[ -d "$STACK/profiles/library" ]] || fail "no profiles checkout at $STACK/profiles. Run the viewer's deploy/setup_ec2.sh first."
  [[ -f "$PKG/pyproject.toml" ]] || fail "this script must run from a senseai-research checkout ($PKG is missing)"

  local VIEWER_ENV=""
  if [[ -f "$STACK/viewer.env" ]]; then
    VIEWER_ENV="$STACK/viewer.env"
  elif [[ -f /etc/senseai-viewer.env ]]; then
    VIEWER_ENV=/etc/senseai-viewer.env
  fi
  echo "    viewer settings: ${VIEWER_ENV:-none found (the viewer will need RESEARCH_URL set by hand)}"

  if [[ "${RESEARCH_SKIP_PIP:-0}" != "1" ]]; then
    command -v uv >/dev/null || fail "uv is not installed (the viewer's setup installs it)"
    step "installing senseai-research into the shared environment"
    # The stack's own checkouts win over the git pins in senseai-research's dependencies.
    local overrides
    overrides="$(mktemp)"
    local p name
    for r in ingest tokens profiles world reflex sdk bench; do
      [[ -d "$STACK/$r" ]] || continue
      while IFS= read -r p; do
        grep -q '^\[project\]' "$p" || continue
        name="$("$PY" -c 'import sys, tomllib; print(tomllib.load(open(sys.argv[1], "rb"))["project"]["name"])' "$p")"
        echo "$name @ file://$(cd "$(dirname "$p")" && pwd)" >>"$overrides"
      done < <(find "$STACK/$r" -maxdepth 3 -name pyproject.toml -not -path '*/node_modules/*' -not -path '*/.venv/*' -not -path '*/tests/*' | sort)
    done
    uv pip install -q --python "$PY" --override "$overrides" -e "$PKG"
    rm -f "$overrides"
  fi
  "$PY" -c "import senseai_research, senseai_profiles" ||
    fail "senseai-research does not import in $VENV"

  step "writing $STACK/research.env"
  mkdir -p "$DATA"
  local RENV="$STACK/research.env"
  if [[ ! -f "$RENV" ]]; then
    cat >"$RENV" <<EOF
# senseai-research settings (read by research-ctl.sh after ${VIEWER_ENV:-the viewer settings}).
RESEARCH_PORT=$PORT
RESEARCH_DATA=$DATA
SENSEAI_PROFILES_REPO=$STACK/profiles
SENSEAI_STACK_DIR=$STACK

# Outside services and their keys. Uncomment and fill in the ones you have; docs/APIS.md says
# what each is for, and "senseai-research settings --check" tests them. The assistant needs
# ANTHROPIC_API_KEY.
EOF
    "$VENV/bin/senseai-research" settings --env | sed -e 's/^\([A-Z]\)/# \1/' >>"$RENV"
    echo "    created (edit it to add ANTHROPIC_API_KEY and the other keys; see docs/APIS.md)"
  else
    echo "    exists, left as is"
  fi

  step "importing the library into the ledger (no model is called)"
  (
    set -a
    # shellcheck disable=SC1090
    [[ -n "$VIEWER_ENV" && -r "$VIEWER_ENV" ]] && . "$VIEWER_ENV"
    # shellcheck disable=SC1090
    . "$RENV"
    set +a
    "$VENV/bin/senseai-research" import
  )

  step "installing research-ctl.sh"
  mkdir -p "$HOME/.local/bin"
  sed -e "s#@STACK@#$STACK#g" "$HERE/research-ctl.sh" >"$HOME/.local/bin/research-ctl.sh"
  chmod +x "$HOME/.local/bin/research-ctl.sh"
  echo "    $HOME/.local/bin/research-ctl.sh"

  step "pointing the viewer at the research server"
  local LINE="RESEARCH_URL=http://127.0.0.1:$PORT"
  if [[ -z "$VIEWER_ENV" ]]; then
    VIEWER_ENV="$STACK/viewer.env"
    echo "    no viewer settings file was found; writing $VIEWER_ENV"
  fi
  if grep -q '^RESEARCH_URL=' "$VIEWER_ENV" 2>/dev/null; then
    echo "    $VIEWER_ENV already sets RESEARCH_URL"
  elif [[ -w "$VIEWER_ENV" || (! -e "$VIEWER_ENV" && -w "$(dirname "$VIEWER_ENV")") ]]; then
    echo "$LINE" >>"$VIEWER_ENV"
    echo "    added $LINE to $VIEWER_ENV"
  elif sudo -n true 2>/dev/null; then
    echo "$LINE" | sudo -n tee -a "$VIEWER_ENV" >/dev/null
    echo "    added $LINE to $VIEWER_ENV"
  else
    echo "    cannot write $VIEWER_ENV; add this line to it by hand: $LINE"
  fi
  local VPKG="$STACK/viewer/packages/senseai-viewer/server/senseai_viewer"
  if [[ -d "$VPKG" && ! -f "$VPKG/research.py" ]]; then
    echo "    note: the viewer in $STACK/viewer has no Research page yet (it arrives with"
    echo "    senseai-institute/viewer PR 'Research page'); update the viewer, then restart it."
  fi
  if systemctl is-active -q senseai-viewer 2>/dev/null && sudo -n true 2>/dev/null; then
    step "restarting the viewer so it reads RESEARCH_URL"
    sudo -n systemctl restart senseai-viewer
  else
    echo "    restart the viewer so it reads RESEARCH_URL (sudo systemctl restart senseai-viewer)"
  fi

  step "done. Start the research server with: research-ctl.sh start"
}

step() { echo "==> $*"; }
fail() {
  echo "==> failed: $*" >&2
  exit 1
}

main "$@"
