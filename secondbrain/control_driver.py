"""Host control backend via the Cua Driver CLI.

The Cua Driver drives your REAL Mac in the background (no cursor/focus steal)
and exposes its tools both as an MCP server and a plain CLI:

    cua-driver call list_windows
    cua-driver call get_window_state '{"pid":P,"window_id":W}'
    cua-driver call click '{"pid":P,"window_id":W,"element_index":N}'
    ...

We run our own ReAct-style loop over that CLI. The crucial property: because
*we* dispatch each action, every step passes through the approval gate BEFORE
it runs, and every step is logged + versioned. The model chooses one tool call
per turn; we gate it, execute it, feed the result back, and repeat.

This needs:
  - the Cua Driver installed (the `cua-driver` binary) + its daemon running
  - litellm + a model (local Ollama or cloud) to drive the loop
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from .config import Config
from .core import RunResult, Step
from .logging import RunLogger
from .models import ModelRouter
from .rules import Action, ApprovalGate, Decision, RuleSet

# Tools the model is allowed to choose. Discovered at runtime when possible,
# with this as a safe fallback matching the documented Cua Driver surface.
_FALLBACK_TOOLS = [
    "list_windows", "launch_app", "get_window_state",
    "click", "type_text_in", "type_text", "scroll_in", "hotkey", "screenshot",
]


def driver_bin() -> Optional[str]:
    """Locate the cua-driver binary (PATH or the standard symlink)."""
    found = shutil.which("cua-driver")
    if found:
        return found
    candidate = Path.home() / ".local" / "bin" / "cua-driver"
    return str(candidate) if candidate.exists() else None


def daemon_running(binary: str) -> bool:
    try:
        out = subprocess.run([binary, "status"], capture_output=True, text=True, timeout=10)
        return out.returncode == 0 and "running" in (out.stdout + out.stderr).lower()
    except Exception:
        return False


def ensure_daemon(binary: str, logger: Optional[RunLogger] = None) -> bool:
    """Start the Cua Driver daemon if needed (macOS bundle-attributed launch)."""
    if daemon_running(binary):
        return True
    try:
        # The `open -n -g -a CuaDriver --args serve` form makes TCC grants
        # attach to CuaDriver.app rather than the parent terminal.
        subprocess.run(["open", "-n", "-g", "-a", "CuaDriver", "--args", "serve"],
                       capture_output=True, text=True, timeout=15)
    except Exception:
        try:
            subprocess.Popen([binary, "serve"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as exc:
            if logger:
                logger.log_event("driver_daemon_error", {"message": str(exc)})
            return False
    for _ in range(20):
        if daemon_running(binary):
            return True
        time.sleep(0.5)
    return False


def discover_tools(binary: str) -> List[str]:
    try:
        out = subprocess.run([binary, "list-tools", "--json"],
                             capture_output=True, text=True, timeout=10)
        data = json.loads(out.stdout)
        names = []
        if isinstance(data, list):
            for t in data:
                names.append(t.get("name") if isinstance(t, dict) else str(t))
        elif isinstance(data, dict):
            names = list((data.get("tools") or {}))
        names = [n for n in names if n]
        return names or _FALLBACK_TOOLS
    except Exception:
        return _FALLBACK_TOOLS


def driver_call(binary: str, tool: str, args: dict, timeout: int,
                screenshot_out: Optional[str] = None) -> Tuple[bool, str]:
    cmd = [binary, "call", tool, json.dumps(args)]
    if screenshot_out:
        cmd += ["--screenshot-out-file", screenshot_out]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        body = (out.stdout or "") + (("\n" + out.stderr) if out.stderr else "")
        return out.returncode == 0, body.strip()
    except subprocess.TimeoutExpired:
        return False, f"timeout after {timeout}s"
    except Exception as exc:
        return False, str(exc)


# Tools that only gather state — the model often loops on these unless nudged.
_OBSERVE_TOOLS = frozenset({"list_windows", "get_window_state", "screenshot"})
_ACTION_TOOLS = (
    "launch_app, click, type_text_in, type_text, scroll_in, hotkey"
)

_SYSTEM_TEMPLATE = """You control a macOS computer through the Cua Driver.
Work one step at a time. On each turn, respond with ONLY a single JSON object,
no prose, in one of these forms:

  {{"thought": "...", "tool": "<tool_name>", "args": {{ ... }}}}
  {{"thought": "...", "tool": "done", "summary": "<what you accomplished>"}}

Available tools: {tools}

Important workflow (do NOT skip actions):
1. The run ALREADY started with list_windows — do NOT call list_windows again.
2. If the target app is not open: launch_app with bundle_id (e.g. com.apple.calculator).
3. Call get_window_state ONCE for that app's (pid, window_id) to get element_index numbers.
4. Then IMMEDIATELY act: click, type_text_in, scroll_in, or hotkey. Do not snapshot again
   unless the UI changed after your last action.
5. After each action, one get_window_state is enough to verify — then act again or done.

Never call get_window_state or list_windows more than twice in a row. Prefer acting.

Tool args:
- launch_app: {{"bundle_id": "com.apple.Safari"}}
- get_window_state: {{"pid": P, "window_id": W}}
- click: {{"pid": P, "window_id": W, "element_index": N}} or pixel {{"x": X, "y": Y}}
- type_text_in: {{"pid": P, "window_id": W, "element_index": N, "text": "..."}}
- hotkey: {{"pid": P, "keys": ["cmd", "q"]}}

Stop with "done" when the goal is met. Keep args minimal and valid JSON.

{rules}
"""


def _extract_json(text: str) -> Optional[dict]:
    """Pull the first JSON object out of a model response, tolerantly."""
    text = text.strip()
    # Strip code fences if present.
    text = re.sub(r"^```(json)?|```$", "", text, flags=re.MULTILINE).strip()
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start:i + 1])
                except Exception:
                    return None
    return None


def run_driver_task(
    config: Config,
    task: str,
    model_key: str,
    ruleset: RuleSet,
    gate: ApprovalGate,
    logger: RunLogger,
    on_step: Optional[Callable[[Step], None]] = None,
) -> RunResult:
    run_id = logger.run_id or ""
    binary = driver_bin()
    if not binary:
        raise RuntimeError(
            "cua-driver not found. Install the Cua Driver and ensure "
            "~/.local/bin is on PATH. See https://cua.ai/docs/cua-driver"
        )
    if not ensure_daemon(binary, logger):
        raise RuntimeError(
            "Cua Driver daemon is not running and could not be started. "
            "Try: open -n -g -a CuaDriver --args serve   (then grant "
            "Accessibility + Screen Recording to CuaDriver.app)."
        )

    tools = discover_tools(binary)
    logger.log_event("driver_ready", {"binary": binary, "tools": tools})

    router = ModelRouter(config)
    system = _SYSTEM_TEMPLATE.format(tools=", ".join(tools), rules=ruleset.system_prompt or "")

    # Seed context with the current windows.
    ok, windows = driver_call(binary, "list_windows", {}, config.step_timeout)
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": (
            f"Goal: {task}\n\n"
            f"Windows already listed (do NOT call list_windows again):\n{windows[:3500]}\n\n"
            "First JSON step: launch_app if the target app is not open, else get_window_state "
            "for ONE window, else click/type. Prefer acting over more observation."
        )},
    ]

    steps: List[Step] = []
    summary = ""
    consecutive_observe = 0
    list_windows_used = True  # we already called it when seeding context

    for i in range(1, config.max_steps + 1):
        completion = router.complete(model_key, messages, temperature=0.1)
        logger.log_event("model_response", {
            "index": i, "model_key": model_key, "model": completion.model,
            "latency_s": completion.latency_s, "cost_usd": completion.cost_usd,
            "error": completion.error, "text": completion.text,
        })
        if not completion.ok:
            summary = f"model error: {completion.error}"
            logger.log_event("driver_abort", {"reason": summary})
            break

        decision_obj = _extract_json(completion.text)
        if not decision_obj or "tool" not in decision_obj:
            messages.append({"role": "user", "content":
                             "Invalid response. Reply with ONLY the JSON object."})
            logger.log_event("parse_retry", {"index": i, "text": completion.text[:500]})
            continue

        tool = str(decision_obj.get("tool"))
        thought = str(decision_obj.get("thought", ""))

        if tool == "done":
            summary = str(decision_obj.get("summary", "task complete"))
            steps.append(Step(i, f"done: {summary}", Decision.ALLOW.value, "model finished", True, summary))
            if on_step:
                on_step(steps[-1])
            break

        args = decision_obj.get("args", {}) or {}

        # Block redundant list_windows (already provided at run start).
        if tool == "list_windows":
            consecutive_observe += 1
            step = Step(i, "list_windows (skipped)", Decision.ALLOW.value,
                        "already have window list from run start", False,
                        output="Skipped: windows were listed at startup. Use launch_app or get_window_state for one target window, then click/type.")
            steps.append(step)
            if on_step:
                on_step(step)
            logger.log_event("driver_skip", {"tool": tool, "reason": "duplicate list_windows"})
            messages.append({"role": "assistant", "content": completion.text})
            messages.append({"role": "user", "content":
                "list_windows was already run at start. Do NOT call it again. "
                f"Next: launch_app if needed, then ONE get_window_state, then an ACTION "
                f"({ _ACTION_TOOLS }). Reply with JSON only."})
            continue

        action = Action(tool=tool, description=thought or f"{tool} {args}", args=args)
        verdict = gate.check(action)
        logger.log_event("action_gate", {
            "index": i, "tool": tool, "args": args,
            "decision": verdict.decision.value, "reason": verdict.reason,
        })
        if verdict.decision is not Decision.ALLOW:
            step = Step(i, f"{tool} {args}", verdict.decision.value, verdict.reason, False)
            steps.append(step)
            if on_step:
                on_step(step)
            summary = f"stopped: action '{tool}' was {verdict.decision.value} ({verdict.reason})"
            logger.log_event("driver_blocked", {"index": i, "tool": tool})
            break

        # Execute the gated action through the driver.
        shot = None
        if tool in ("get_window_state", "screenshot"):
            shot = str(config.logs_dir / f"{run_id}_step{i}.png")
        ok, result = driver_call(binary, tool, args, config.step_timeout, screenshot_out=shot)
        step = Step(i, f"{tool} {args}", Decision.ALLOW.value, "executed", True,
                    output=result[:1000])
        steps.append(step)
        if on_step:
            on_step(step)
        logger.log_event("driver_result", {"index": i, "tool": tool, "ok": ok,
                                            "output": result[:4000], "screenshot": shot})

        if tool in _OBSERVE_TOOLS:
            consecutive_observe += 1
        else:
            consecutive_observe = 0

        messages.append({"role": "assistant", "content": completion.text})
        follow = (
            f"Result of {tool} (ok={ok}):\n{result[:4000]}\n\n"
            "Next action as JSON, or done."
        )
        if consecutive_observe >= 2:
            follow = (
                f"Result of {tool} (ok={ok}):\n{result[:2500]}\n\n"
                "STOP re-reading the UI. You already have enough state. "
                f"Your NEXT step MUST be an action tool ({_ACTION_TOOLS}) or done — "
                "NOT list_windows or another get_window_state unless you just changed the UI."
            )
        messages.append({"role": "user", "content": follow})
    else:
        summary = summary or f"reached max_steps ({config.max_steps}) without finishing"

    return RunResult(run_id, "completed", summary or "driver task finished",
                     model_key, ruleset.name, steps)
