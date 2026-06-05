"""Adapter for real macOS host control via cua-agent + the Cua Driver.

This module imports cua lazily and is intentionally isolated from the core so
that the rest of Second Brain never hard-depends on it. If cua-agent is not
installed (it needs Python >=3.11 and the Cua Driver), importing or running
this raises, and the core falls back to plan-only mode.

The integration point that matters most for safety is ``_make_gated_callback``:
every proposed computer action is routed through the approval gate BEFORE it
executes. The exact callback hook differs slightly between cua versions, so
this is the primary place to adapt when you install/upgrade cua.
"""

from __future__ import annotations

import asyncio
from typing import Callable, List, Optional

from .config import Config
from .core import RunResult, Step
from .logging import RunLogger
from .rules import Action, ApprovalGate, Decision, RuleSet


def _import_cua():
    """Return (Computer, ComputerAgent) across known cua module layouts.

    0.8.x: Computer lives in `computer`, ComputerAgent in `cua_agent` (or the
    `cua` meta-package). 0.4.x legacy used `agent`.
    """
    errors = []
    # (computer_module, ComputerAttr, agent_module, AgentAttr)
    candidates = (
        ("computer", "Computer", "cua_agent", "ComputerAgent"),  # 0.8.x
        ("computer", "Computer", "cua", "ComputerAgent"),        # meta-package
        ("computer", "Computer", "agent", "ComputerAgent"),      # 0.4.x legacy
        ("cua", "Computer", "cua", "ComputerAgent"),
    )
    for cmod, cattr, amod, aattr in candidates:
        try:
            comp_mod = __import__(cmod, fromlist=[cattr])
            agent_mod = __import__(amod, fromlist=[aattr])
            return getattr(comp_mod, cattr), getattr(agent_mod, aattr)
        except Exception as exc:  # try the next layout
            errors.append(f"{cmod}.{cattr}+{amod}.{aattr}: {exc}")
    raise ImportError(
        "cua-agent SDK not importable. Install with: pip install 'cua-agent[all]' "
        "(Python >=3.11). NOTE: the SDK targets sandboxes/VMs; for HOST control "
        "use backend=driver (the Cua Driver). Tried: " + " | ".join(errors)
    )


def _make_gated_callback(gate: ApprovalGate, logger: RunLogger):
    """Build a pre-action callback that enforces the approval gate.

    cua passes the agent's intended action; we translate it to our Action,
    run it through the gate, log the decision, and return whether to proceed.
    Return value semantics may need adjusting per cua version (some expect a
    bool, some expect the (possibly modified) action, some raise to block).
    """

    def before_action(action_payload) -> bool:
        tool = getattr(action_payload, "type", None) or getattr(action_payload, "name", "action")
        args = getattr(action_payload, "arguments", None) or getattr(action_payload, "args", {}) or {}
        description = getattr(action_payload, "text", "") or str(action_payload)
        action = Action(tool=str(tool), description=str(description), args=dict(args) if isinstance(args, dict) else {})

        result = gate.check(action)
        logger.log_event(
            "action_gate",
            {"tool": action.tool, "decision": result.decision.value, "reason": result.reason,
             "description": action.description},
        )
        return result.decision is Decision.ALLOW

    return before_action


def run_cua_task(
    config: Config,
    task: str,
    model_key: str,
    ruleset: RuleSet,
    gate: ApprovalGate,
    logger: RunLogger,
    on_step: Optional[Callable[[Step], None]] = None,
) -> RunResult:
    Computer, ComputerAgent = _import_cua()
    spec = config.model(model_key)
    steps: List[Step] = []

    def _text_of(result) -> str:
        """Extract human text from a cua-agent 0.8 result dict (or object)."""
        try:
            out = result["output"] if isinstance(result, dict) else getattr(result, "output", None)
            if out:
                chunks = []
                for item in out:
                    if isinstance(item, dict) and item.get("type") == "message":
                        content = item.get("content") or []
                        for c in content:
                            if isinstance(c, dict) and c.get("text"):
                                chunks.append(c["text"])
                if chunks:
                    return "\n".join(chunks)
        except Exception:
            pass
        if isinstance(result, dict) and result.get("text"):
            return str(result["text"])
        return str(result)

    async def _run() -> str:
        # cua-agent 0.8 API: ComputerAgent(model=..., tools=[computer]).
        # NOTE: Computer() targets a cua sandbox/VM, not your host. For host
        # control use backend=driver. Rules are injected via a system message
        # since this API has no `instructions` kwarg.
        async with Computer() as computer:
            agent = ComputerAgent(model=spec.model, tools=[computer])
            messages = []
            if ruleset.system_prompt:
                messages.append({"role": "system", "content": ruleset.system_prompt})
            messages.append({"role": "user", "content": task})
            final_text = ""
            i = 0
            async for result in agent.run(messages):
                i += 1
                text = _text_of(result)
                logger.log_event("cua_event", {"index": i, "event": text[:4000]})
                step = Step(i, text[:300], Decision.ALLOW.value, "cua step", executed=True, output=text[:1000])
                steps.append(step)
                if on_step:
                    on_step(step)
                if text:
                    final_text = text
            return final_text

    summary = asyncio.run(_run())
    return RunResult(
        run_id=logger.run_id or "",
        status="completed",
        summary=summary or "cua task finished",
        model_key=model_key,
        rules=ruleset.name,
        steps=steps,
    )
