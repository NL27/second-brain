# Second Brain — Personal Computer-Control Agent

Phase 1 of a personal AI operating system: an **integrate-first, open-source agent that sees and drives your real Mac in the background**, routes tasks across **local + cloud LLMs** with head-to-head evaluation, and **logs and versions everything** under a small core you own.

This is the protected foundation. Later phases (knowledge base, scheduling, messaging, voice, n8n) plug into the same core via **MCP**.

## What it does today (Phase 1)

- **Host control with guardrails** — drives your actual desktop apps via the **Cua Driver** (background; does not steal your cursor or focus). Second Brain runs the action loop itself over the `cua-driver` CLI, so every proposed click/keystroke passes through an **approval gate** before it executes.
- **Multi-LLM routing + evaluation** — one interface to local (Ollama) and cloud (Claude/GPT/Groq/DeepSeek/Qwen/Gemini) models via liteLLM. Run the *same task across models* and rank them.
- **Per-task rules** — YAML rule files define instructions, allow/deny lists, and what counts as destructive. Fork bombs and credential paths are hard-blocked; deletes/sends/`sudo` require confirmation.
- **Everything logged + versioned** — each run is a JSONL trajectory, indexed in SQLite, and **auto-committed to git** so any run can be replayed and understood afterward.
- **Plan-only fallback** — with no control tooling or API keys installed, the whole pipeline still runs (the model produces a plan; nothing executes), so you can try it instantly.

## Architecture

```mermaid
flowchart TD
    User["You (CLI / chat)"] --> Core["Agent core"]
    Core --> Rules["Rules + approval gate"]
    Core --> Router["Model router (liteLLM)"]
    Core --> Logger["Logger + versioner"]
    Rules -->|"allow / confirm / deny"| Tools["Tools"]
    Router --> Local["Ollama (local)"]
    Router --> Cloud["Cloud LLMs"]
    Tools --> Driver["Cua Driver -> your macOS desktop"]
    Logger --> Store["JSONL + SQLite + git"]
```

## Quick start

```bash
# 0. On any machine, check what's needed first (stdlib only, no install required)
python3 scripts/check_system.py        # add --json for machine-readable, --install to auto-install

# Instant try (plan-only, no keys needed)
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
brain doctor
brain run-task "open Notes and write a hello note" --dry-run

# Full host control + local models (macOS, Python 3.9+)
bash scripts/setup.sh                  # runs the system check, installs the Cua Driver + deps
# then grant Accessibility + Screen Recording to BOTH CuaDriver.app and your terminal/IDE
brain doctor                           # "Cua Driver (host control)" should be ok
brain run-task "open Calculator and press 7"
```

### Running on a different machine

`scripts/check_system.py` is dependency-free and portable (macOS / Linux / Windows). Copy the repo (or just that file) to a new box and run it before installing anything. It detects OS, CPU/RAM, Python version, installed tools (git, Ollama, node, docker...), running services, and cloud keys, then prints prioritized, copy-pasteable install steps tailored to that machine (e.g. which local models its RAM can handle, whether host control is supported). Once the package is installed, the same report is available as `brain check`.

## Usage

```bash
brain check                          # portable system check + install recommendations
brain doctor                         # environment check
brain models                         # list model registry + availability
brain run-task "..."                 # run a task (gated, logged, versioned)
brain run-task "..." -m cloud-claude # pick a model
brain run-task "..." -r destructive  # use a stricter rule set
brain run-task "..." --dry-run       # plan + log only, never execute
brain chat                           # interactive task loop
brain eval "..." --models local-vision,cloud-claude --judge cloud-gpt
brain runs                           # recent versioned runs
brain ui                             # optional Gradio chat UI
```

## Configuration

- `config.yaml` (optional, project root) overrides the packaged defaults in `secondbrain/default_config.yaml` — model registry, default model, control backend (`cua` or `none`), and safety switches.
- `.env` (copy from `.env.example`) holds cloud API keys. Local models need none.
- `rules/*.yaml` define per-task policy. `default.yaml` applies to every task; pass `--rules <name>` to layer another on top.

### Safety model

- The agent works in the **background** (Cua Driver), so it can't hijack your live session.
- Destructive actions (deletes, `sudo`, sends, payments, installs) require **explicit confirmation**; a deny-list hard-blocks the truly dangerous.
- Actions are **logged before execution** and git-committed, so every run is auditable and replayable. Set `--dry-run` (or `safety.dry_run: true`) for plan-only.

## Control backends

Set `control.backend` in `config.yaml`:

- `driver` (default) — host control of your **real Mac** via the **Cua Driver** CLI. Second Brain runs the gated loop. Works on **Python 3.9+**. Needs the `cua-driver` binary + liteLLM + a model.
- `cua` — the **cua-agent SDK** driving a cua **sandbox/VM** (not your host). Needs **Python 3.11+** and `pip install 'cua-agent[all]'`.
- `none` — plan-only (no execution). Cross-platform, no extra deps.

For the `driver` backend, start the daemon once so macOS attributes permissions to the app bundle:

```bash
open -n -g -a CuaDriver --args serve     # macOS
```

and grant **both** `CuaDriver.app` and your terminal/IDE Accessibility + Screen Recording.

## Using your Cursor subscription as the model brain

Low-RAM machine? Instead of running a big local model, you can drive the agent
with Cursor's cloud models via the Cursor SDK:

```bash
pip install cursor-sdk
# put CURSOR_API_KEY=... in .env  (Cursor Dashboard -> Integrations)
brain run-task "open Calculator and press 7" -m cursor
```

The `cursor` model (`cursor/auto`) routes through `cursor-sdk`. It fits the
`driver` backend well because that loop reasons over the accessibility tree as
text. Note: the Cursor SDK runs an agent per step, so it's a bit slower than a
raw API key; for a snappier loop use a dedicated cloud key.

## Requirements

- **Host control**: macOS 14+ (or Windows; Linux pre-release) with the **Cua Driver** installed, plus Python **3.9+** and liteLLM.
- **Plan-only / eval**: any OS, Python 3.9+.
- **Optional** `cua` sandbox backend: Python 3.11+ and `cua-agent`.
- Optional: Ollama for local models; cloud API keys for cloud models.

## Project layout

- `secondbrain/` — the core package (config, models, rules, logging, core, eval, cli).
- `secondbrain/control_driver.py` — host control loop over the Cua Driver CLI (the `driver` backend).
- `secondbrain/control_cua.py` — the `cua` sandbox/VM backend (cua-agent SDK).
- `rules/` — per-task policy files.
- `mcp/servers.json` — MCP server registry (the extensibility backbone).
- `logs/` — versioned run trajectories.
- `scripts/check_system.py` — portable, dependency-free system check + recommendations (run on any machine).
- `scripts/setup.sh` — one-time host setup (runs the check first).
- `docs/EXTENDING.md` — how to add tools/phases without touching the core.

## Roadmap

- **Phase 2 — Second brain:** notes + hashtag graph, YouTube/article summaries, chat with your knowledge (vector search MCP).
- **Phase 3 — Tasks & scheduling:** todos from conversations; meetings with location/time/travel (calendar + maps MCP).
- **Phase 4 — Messaging:** Telegram/Slack/etc. as channels.
- **Phase 5 — Voice + n8n + public extensibility.**

See [docs/EXTENDING.md](docs/EXTENDING.md) for how each plugs into the core.

## License

MIT.
