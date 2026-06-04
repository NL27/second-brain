# Extending Second Brain

The whole point of the architecture is that the **core stays small and stable**, and everything else plugs in around it. There are three extension surfaces. Pick the lowest one that does the job.

## 1. Rules (no code) — change behavior and safety

Add a YAML file in `rules/` and select it with `--rules <name>`. Rules layer on top of `default.yaml`.

```yaml
# rules/research.yaml
name: research
system_prompt: |
  Focus on reading and summarizing. Never modify files or send messages.
deny_patterns:
  - "\\bsend\\b"
  - "\\brm\\b"
```

Use it: `brain run-task "summarize the open PDF" --rules research`.

## 2. MCP servers (config) — give the agent new tools

MCP (Model Context Protocol) is the interoperability backbone. Any capability — host control, shell, a knowledge base, n8n, a calendar — is just an MCP server the agent (or any MCP client like Cursor/Claude) connects to. Register servers in `mcp/servers.json` and flip `enabled` to `true`.

```jsonc
"n8n": {
  "enabled": true,
  "command": "npx",
  "args": ["-y", "n8n-mcp"],
  "env": { "N8N_BASE_URL": "http://localhost:5678", "N8N_API_KEY": "..." }
}
```

This is how later phases attach without touching the core:
- Phase 2 knowledge base -> a vector-search MCP server (`knowledge`).
- Phase 3 scheduling -> calendar + maps MCP servers (`calendar`).
- Phase 4 messaging -> a Telegram/Slack MCP server.
- Phase 5 automation -> the `n8n` MCP server.

Every tool call still flows through the rules/approval gate and the logger.

## 3. Code (Python) — new backends or models

The core (`secondbrain/`) exposes a few stable seams:

- **Add a model**: edit the `models:` block in `config.yaml`. Use liteLLM provider syntax (`ollama/...`, `openai/...`, `anthropic/...`). No code needed.
- **Add a control backend**: implement a module like `secondbrain/control_cua.py` exposing a `run_*_task(config, task, model_key, ruleset, gate, logger, on_step)` function, and dispatch to it from `Agent._run_*` in `core.py`. Keep host-control imports lazy so the core never hard-depends on them.
- **Tighten the gate**: the single safety chokepoint is `ApprovalGate.check` in `rules.py`. Any executor must call it before acting. In the cua adapter this is `_make_gated_callback`.

### Contract for a new backend

```python
def run_x_task(config, task, model_key, ruleset, gate, logger, on_step=None) -> RunResult:
    # 1. for each intended action:
    #    decision = gate.check(Action(tool=..., description=..., args=...))
    #    logger.log_event("action_gate", {...})
    #    if decision.decision is not Decision.ALLOW: skip
    # 2. execute allowed actions, logger.log_event(...) each result
    # 3. return RunResult(run_id=logger.run_id, status=..., summary=..., ...)
```

Because gating and logging are passed in, any backend you add inherits the safety and versioning guarantees for free.

## Design rules (keep the core protected)

1. The core never imports optional/host-control dependencies at module top level — always lazily, inside the adapter.
2. Every action passes through the approval gate before it runs.
3. Every run is logged and versioned; failures still finalize the log.
4. Prefer rules (no code) > MCP (config) > Python. Only touch `core.py` for genuinely new backends.
