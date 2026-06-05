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

# 4. Host control via the Cua Driver (works on Python 3.9+) ------------------
say "Installing the Cua Driver (drives your real Mac in the background)..."
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/trycua/cua/main/libs/cua-driver/scripts/install.sh)" \
  || warn "Cua Driver install skipped/failed. See https://cua.ai/docs/cua-driver"

if command -v cua-driver >/dev/null 2>&1 || [ -x "$HOME/.local/bin/cua-driver" ]; then
  DRIVER="$(command -v cua-driver || echo "$HOME/.local/bin/cua-driver")"
  say "Configuring Cua Driver capture mode (som = AX tree + screenshot)..."
  "$DRIVER" config set capture_mode som || true
  say "Starting the Cua Driver daemon..."
  open -n -g -a CuaDriver --args serve 2>/dev/null || "$DRIVER" serve >/dev/null 2>&1 &
fi

# 4b. Optional: cua-agent SDK (sandbox/VM backend) - only needs/uses 3.11+ ----
if [ "$(printf '%s\n3.11\n' "$PYV" | sort -V | head -1)" = "3.11" ]; then
  say "Installing optional cua-agent SDK (sandbox backend)..."
  pip install -q "cua-agent[all]" || warn "cua-agent SDK install failed (optional; host control unaffected)."
else
  warn "Skipping optional cua-agent SDK (needs Python >= 3.11). Host control via the Cua Driver still works."
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
echo "  1) Grant BOTH CuaDriver.app AND your terminal/IDE:"
echo "     System Settings > Privacy & Security > Accessibility AND Screen Recording."
echo "  2) source .venv/bin/activate"
echo "  3) brain doctor      # Cua Driver should show ok (daemon running)"
echo "  4) brain run-task \"open the Calculator and press 7\""
