"""The agent core - the small, stable piece you own.

It coordinates four things for every task:
  1. the model router (which LLM, local or cloud)
  2. the rules + approval gate (what is allowed / needs confirmation)
  3. the run logger (trajectory + SQLite + git versioning)
  4. the control backend (cua host control, or plan-only fallback)

Backends:
  - "cua":  drives the real Mac via cua-agent + the Cua Driver. Every
            proposed action passes through the approval gate before it runs.
  - "none": plan-only. The model produces a step plan; each step is
            classified by the rules and logged, but nothing is executed.
            This makes the whole pipeline runnable without any control
            tooling or API keys installed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable, List, Optional

from .config import Config
from .logging import RunLogger
from .models import ModelRouter
from .rules import Action, ApprovalGate, Decision, GateResult, RuleSet, load_ruleset


@dataclass
class Step:
    index: int
    description: str
    decision: str
    reason: str
    executed: bool = False
    output: str = ""


@dataclass
class RunResult:
    run_id: str
    status: str
    summary: str
    model_key: str
    rules: str
    steps: List[Step] = field(default_factory=list)


# A confirmer takes a GateResult and returns True to proceed.
Confirmer = Callable[[GateResult], bool]


def _auto_deny(_: GateResult) -> bool:
    return False


class Agent:
    """High-level entry point: ``Agent(config).run_task("...")``."""

    def __init__(self, config: Config, confirmer: Optional[Confirmer] = None):
        self.config = config
        self.router = ModelRouter(config)
        self.confirmer = confirmer or _auto_deny

    # -- public API --------------------------------------------------------
    def run_task(
        self,
        task: str,
        model_key: Optional[str] = None,
        rules_name: Optional[str] = None,
        on_step: Optional[Callable[[Step], None]] = None,
    ) -> RunResult:
        model_key = model_key or self.config.default_model
        ruleset = load_ruleset(self.config, rules_name)
        gate = ApprovalGate(ruleset, self.confirmer)

        logger = RunLogger(self.config)
        run_id = logger.start_run(task, model_key, ruleset.name, meta={
            "backend": self.config.control_backend,
            "dry_run": self.config.dry_run,
        })

        try:
            backend = self.config.control_backend
            if self.config.dry_run or backend == "none":
                result = self._run_plan_only(task, model_key, ruleset, gate, logger, on_step)
            elif backend == "driver":
                result = self._run_driver(task, model_key, ruleset, gate, logger, on_step)
            elif backend == "cua":
                result = self._run_cua(task, model_key, ruleset, gate, logger, on_step)
            else:
                result = self._run_plan_only(task, model_key, ruleset, gate, logger, on_step)
            logger.finish_run(result.status, result.summary)
            return result
        except Exception as exc:  # never let a crash skip versioning
            logger.log_event("error", {"message": str(exc)})
            logger.finish_run("failed", f"error: {exc}")
            return RunResult(run_id, "failed", str(exc), model_key, ruleset.name)
        finally:
            logger.close()

    # -- backends ----------------------------------------------------------
    def _run_plan_only(
        self,
        task: str,
        model_key: str,
        ruleset: RuleSet,
        gate: ApprovalGate,
        logger: RunLogger,
        on_step: Optional[Callable[[Step], None]],
    ) -> RunResult:
        run_id = logger.run_id or ""
        messages = [
            {"role": "system", "content": self._system_prompt(ruleset)},
            {
                "role": "user",
                "content": (
                    f"Task: {task}\n\n"
                    "Produce a concise, numbered, step-by-step plan to accomplish "
                    "this on a macOS computer. One action per line. Do not execute "
                    "anything; just plan."
                ),
            },
        ]
        completion = self.router.complete(model_key, messages)
        logger.log_event(
            "model_response",
            {
                "model_key": model_key,
                "model": completion.model,
                "latency_s": completion.latency_s,
                "cost_usd": completion.cost_usd,
                "error": completion.error,
                "text": completion.text,
            },
        )

        if not completion.ok:
            # Deterministic stub so the pipeline is demonstrable offline.
            plan_lines = self._stub_plan(task)
            logger.log_event("plan_fallback", {"reason": completion.error})
        else:
            plan_lines = self._parse_plan(completion.text)

        steps: List[Step] = []
        for i, line in enumerate(plan_lines, start=1):
            action = Action(tool="plan_step", description=line)
            verdict = gate.evaluate(action)  # classify only; nothing runs
            step = Step(i, line, verdict.decision.value, verdict.reason, executed=False)
            steps.append(step)
            logger.log_event(
                "plan_step",
                {"index": i, "description": line, "decision": verdict.decision.value,
                 "reason": verdict.reason},
            )
            if on_step:
                on_step(step)

        gated = sum(1 for s in steps if s.decision != Decision.ALLOW.value)
        summary = (
            f"Planned {len(steps)} steps (plan-only mode; backend="
            f"{self.config.control_backend}, dry_run={self.config.dry_run}). "
            f"{gated} step(s) would require approval/are blocked."
        )
        return RunResult(run_id, "completed", summary, model_key, ruleset.name, steps)

    def _run_driver(
        self,
        task: str,
        model_key: str,
        ruleset: RuleSet,
        gate: ApprovalGate,
        logger: RunLogger,
        on_step: Optional[Callable[[Step], None]],
    ) -> RunResult:
        """Drive the real Mac via the Cua Driver CLI, gating every action.

        Falls back to plan-only (logged) if the driver is unavailable.
        """
        try:
            from .control_driver import run_driver_task
        except Exception as exc:
            logger.log_event("backend_unavailable", {"backend": "driver", "message": str(exc)})
            return self._run_plan_only(task, model_key, ruleset, gate, logger, on_step)
        try:
            return run_driver_task(self.config, task, model_key, ruleset, gate, logger, on_step)
        except Exception as exc:
            logger.log_event("backend_error", {"backend": "driver", "message": str(exc)})
            logger.log_event("backend_fallback", {"to": "plan_only"})
            return self._run_plan_only(task, model_key, ruleset, gate, logger, on_step)

    def _run_cua(
        self,
        task: str,
        model_key: str,
        ruleset: RuleSet,
        gate: ApprovalGate,
        logger: RunLogger,
        on_step: Optional[Callable[[Step], None]],
    ) -> RunResult:
        """Drive a cua sandbox/VM via cua-agent, gating every action.

        Best-effort integration: if cua-agent (and the Cua Driver) are not
        installed, we log the reason and fall back to plan-only so the run
        still completes and is versioned.
        """
        run_id = logger.run_id or ""
        try:
            from .control_cua import run_cua_task  # local adapter, imports cua lazily
        except Exception as exc:
            logger.log_event("backend_unavailable", {"message": str(exc)})
            return self._run_plan_only(task, model_key, ruleset, gate, logger, on_step)

        try:
            return run_cua_task(
                config=self.config,
                task=task,
                model_key=model_key,
                ruleset=ruleset,
                gate=gate,
                logger=logger,
                on_step=on_step,
            )
        except Exception as exc:
            logger.log_event("backend_error", {"message": str(exc)})
            logger.log_event("backend_fallback", {"to": "plan_only"})
            return self._run_plan_only(task, model_key, ruleset, gate, logger, on_step)

    # -- helpers -----------------------------------------------------------
    def _system_prompt(self, ruleset: RuleSet) -> str:
        base = (
            "You are Second Brain, a personal computer-control agent running on "
            "the user's macOS machine."
        )
        return f"{base}\n\n{ruleset.system_prompt}".strip()

    @staticmethod
    def _parse_plan(text: str) -> List[str]:
        lines: List[str] = []
        for raw in text.splitlines():
            line = raw.strip()
            if not line:
                continue
            # Strip leading list markers like "1.", "1)", "-", "*".
            line = re.sub(r"^\s*(\d+[\.\)]|[-*])\s*", "", line)
            if line:
                lines.append(line)
        return lines or [text.strip()]

    @staticmethod
    def _stub_plan(task: str) -> List[str]:
        return [
            f"Interpret the goal: {task}",
            "Capture the current screen to understand context",
            "Identify the target application and UI elements",
            "Perform the required interactions step by step",
            "Verify the result matches the goal",
        ]
