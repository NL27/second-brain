#!/usr/bin/env bash
# Second Brain - one-time host setup for full computer control on macOS.
# Safe to re-run. Run from the project root: bash scripts/setup.sh
set -euo pipefail

say() { printf "\n\033[1;36m==> %s\033[0m\n" "$1"; }
warn() { printf "\033[1;33m! %s\033[0m\n" "$1"; }

# 0. System check ------------------------------------------------------------
say "Checking this system..."
"${PYTHON:-python3}" scripts/check_system.py || warn "System check reported blockers (see above)."

# 1. .env --------------------------------------------------------------------
if [ ! -f .env ]; then
  cp .env.example .env
  say "Created .env from .env.example - add your cloud API keys (optional)."
else
  say ".env already exists - leaving it untouched."
fi

# 2. Python venv (3.11+ required for cua host control) -----------------------
PY="${PYTHON:-python3}"
PYV="$("$PY" -c 'import sys;print("%d.%d"%sys.version_info[:2])')"
say "Using Python $PYV from $(command -v "$PY")"
if [ "$(printf '%s\n3.11\n' "$PYV" | sort -V | head -1)" != "3.11" ]; then
  warn "Python >= 3.11 is required for cua host control. Plan-only mode still"
  warn "works on 3.9/3.10. Install 3.11+ (e.g. 'brew install python@3.12')"
  warn "and re-run as: PYTHON=python3.12 bash scripts/setup.sh"
fi

if [ ! -d .venv ]; then
  "$PY" -m venv .venv
  say "Created .venv"
fi
# shellcheck disable=SC1091
source .venv/bin/activate
pip install -q --upgrade pip

# 3. Install the package + LLM routing ---------------------------------------
say "Installing Second Brain + multi-LLM routing (litellm)..."
pip install -q -e ".[llm]"

# 4. cua host control (only on 3.11+) ----------------------------------------
if [ "$(printf '%s\n3.11\n' "$PYV" | sort -V | head -1)" = "3.11" ]; then
  say "Installing cua-agent (host control). This pulls a few packages..."
  pip install -q "cua-agent[all]" || warn "cua-agent install failed; staying plan-only."
  say "Installing the Cua Driver (background host control, no cursor-steal)..."
  /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/trycua/cua/main/libs/cua-driver/scripts/install.sh)" \
    || warn "Cua Driver install skipped/failed. See https://github.com/trycua/cua"
else
  warn "Skipping cua-agent (needs Python >= 3.11)."
fi

# 5. Ollama + local models ----------------------------------------------------
if command -v ollama >/dev/null 2>&1; then
  say "Pulling local models (UI-TARS for vision/grounding + a general model)..."
  ollama pull ui-tars:7b   || warn "Could not pull ui-tars:7b"
  ollama pull qwen2.5:7b   || warn "Could not pull qwen2.5:7b"
else
  warn "Ollama not found. Install it: 'brew install ollama' then 'ollama serve'."
  warn "Then: ollama pull ui-tars:7b && ollama pull qwen2.5:7b"
fi

say "Done. Next:"
echo "  1) Grant Terminal/IDE: System Settings > Privacy & Security >"
echo "     Accessibility AND Screen Recording."
echo "  2) source .venv/bin/activate"
echo "  3) brain doctor      # verify everything is green"
echo "  4) brain run-task \"open Notes and write 'hello'\" --dry-run"
