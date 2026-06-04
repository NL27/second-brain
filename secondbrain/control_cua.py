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
    """Return (Computer, ComputerAgent) across known cua module layouts."""
    errors = []
    # Newer layout: top-level `cua` plus `agent` packages.
    for computer_path, agent_path in (
        ("computer", "agent"),
        ("cua", "cua_agent"),
        ("cua.computer", "cua.agent"),
    ):
        try:
            comp_mod = __import__(computer_path, fromlist=["Computer"])
            agent_mod = __import__(agent_path, fromlist=["ComputerAgent"])
            return getattr(comp_mod, "Computer"), getattr(agent_mod, "ComputerAgent")
        except Exception as exc:  # try the next layout
            errors.append(f"{computer_path}/{agent_path}: {exc}")
    raise ImportError(
        "cua-agent not available. Install with: pip install 'cua-agent[all]' "
        "(requires Python >=3.11) and the Cua Driver. Tried: " + " | ".join(errors)
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

    gated = _make_gated_callback(gate, logger)
    steps: List[Step] = []

    async def _run() -> str:
        # NOTE: keyword names below follow the documented cua-agent SDK. If a
        # newer version renames them, adjust here - this is the single seam
        # between Second Brain and cua.
        async with Computer() as computer:  # background host control via Cua Driver
            agent = ComputerAgent(
                computer=computer,
                model=spec.model,
                instructions=ruleset.system_prompt or None,
                callbacks=[gated],
                max_trajectory_budget=config.max_steps,
            )
            final_text = ""
            i = 0
            async for event in agent.run(task):
                i += 1
                text = getattr(event, "text", None) or str(event)
                logger.log_event("cua_event", {"index": i, "event": text})
                step = Step(i, text, Decision.ALLOW.value, "cua step", executed=True, output=text)
                steps.append(step)
                if on_step:
                    on_step(step)
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
