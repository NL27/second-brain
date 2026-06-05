"""Cursor SDK provider - use your Cursor subscription's models as the brain.

This lets a low-RAM machine (e.g. an 8 GB M1) drive the agent loop with
Cursor's cloud models instead of a heavy local model. It is exposed in the
model registry as a `cursor/<model_id>` entry (e.g. `cursor/composer-2.5`).

Notes / caveats:
  - Needs `pip install cursor-sdk` and CURSOR_API_KEY (Cursor Dashboard ->
    Integrations).
  - The SDK runs an *agent*, not a plain chat endpoint. We run a one-shot
    `Agent.prompt` against an isolated temp cwd and read back the final text,
    then parse our JSON action out of it. Heavier/slower than a raw API; good
    enough for the text-based (accessibility-tree) driver loop.
"""

from __future__ import annotations

import os
import tempfile
from typing import Dict, List, Optional, Tuple


def _flatten(messages: List[Dict[str, str]]) -> str:
    """Collapse a chat message list into a single prompt string."""
    parts = []
    for m in messages:
        role = m.get("role", "user").upper()
        parts.append(f"[{role}]\n{m.get('content', '')}")
    parts.append(
        "[INSTRUCTION]\nRespond with ONLY the answer the USER asked for. "
        "Do not edit files or use tools; just produce the text/JSON response."
    )
    return "\n\n".join(parts)


def cursor_complete(
    model_id: str,
    messages: List[Dict[str, str]],
) -> Tuple[Optional[str], Optional[str]]:
    """Return (text, error). text is the model's final response."""
    try:
        from cursor_sdk import Agent, AgentOptions, LocalAgentOptions
    except Exception as exc:  # pragma: no cover - optional dependency
        return None, f"cursor-sdk not installed (pip install cursor-sdk): {exc}"

    api_key = os.getenv("CURSOR_API_KEY")
    if not api_key:
        return None, "CURSOR_API_KEY not set (Cursor Dashboard -> Integrations)."

    # Isolated cwd so the agent can't touch your project files.
    cwd = tempfile.mkdtemp(prefix="secondbrain-cursor-")
    try:
        result = Agent.prompt(
            _flatten(messages),
            AgentOptions(
                api_key=api_key,
                model=model_id,
                local=LocalAgentOptions(cwd=cwd),
            ),
        )
    except Exception as exc:
        return None, str(exc)

    status = getattr(result, "status", None)
    text = getattr(result, "result", None)
    if isinstance(result, dict):
        status = status or result.get("status")
        text = text or result.get("result")
    if status == "error":
        return None, f"cursor run error: {text}"
    return (text or ""), None
