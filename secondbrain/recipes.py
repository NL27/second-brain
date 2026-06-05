"""Hardcoded task recipes — reliable, no LLM.

Recipes are YAML files in ``recipes/`` with a fixed sequence of Cua Driver
actions. Use them when you know exactly what should happen (open Safari,
press ⌘L, type a URL, etc.) instead of letting a small model guess element_index.

Example::

    brain recipe list
    brain recipe run safari-google
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import yaml

from .config import Config
from .control_driver import (
    _auto_snapshot,
    _launch_context_hint,
    _parse_launch_context,
    _valid_window_id,
    driver_bin,
    driver_call,
    ensure_daemon,
)
from .core import RunResult, Step
from .logging import RunLogger
from .rules import Action, ApprovalGate, Decision, RuleSet, load_ruleset


@dataclass
class RecipeStep:
    tool: str
    args: Dict[str, Any] = field(default_factory=dict)
    use_launch_context: bool = False
    wait_seconds: float = 0.0
    description: str = ""


@dataclass
class Recipe:
    name: str
    description: str
    steps: List[RecipeStep]
    path: Path


def recipes_dir(config: Config) -> Path:
    d = config.root / "recipes"
    d.mkdir(parents=True, exist_ok=True)
    return d


def list_recipes(config: Config) -> List[Recipe]:
    out: List[Recipe] = []
    for path in sorted(recipes_dir(config).glob("*.yaml")):
        try:
            out.append(load_recipe(path))
        except Exception:
            continue
    return out


def load_recipe(path: Path) -> Recipe:
    with open(path, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    name = data.get("name") or path.stem
    desc = data.get("description", "")
    steps: List[RecipeStep] = []
    for entry in data.get("steps") or []:
        if not isinstance(entry, dict):
            continue
        if "wait_seconds" in entry and len(entry) == 1:
            steps.append(RecipeStep(tool="wait", wait_seconds=float(entry["wait_seconds"]),
                                  description="pause"))
            continue
        tool = entry.get("tool") or entry.get("action")
        if not tool:
            # Shorthand: {launch_app: {bundle_id: ...}}
            if len(entry) == 1:
                tool = next(iter(entry.keys()))
                args = entry[tool] if isinstance(entry[tool], dict) else {}
            else:
                continue
        else:
            args = entry.get("args") or {}
            if not isinstance(args, dict):
                args = {}
        steps.append(RecipeStep(
            tool=str(tool),
            args=dict(args),
            use_launch_context=bool(entry.get("use_launch_context", False)),
            wait_seconds=float(entry.get("wait_seconds") or 0),
            description=str(entry.get("description") or ""),
        ))
    if not steps:
        raise ValueError(f"Recipe '{name}' has no steps: {path}")
    return Recipe(name=name, description=desc, steps=steps, path=path)


def _apply_launch_context(args: dict, ctx: dict) -> dict:
    out = dict(args)
    if ctx.get("pid") is not None:
        out.setdefault("pid", ctx["pid"])
    if ctx.get("window_ids"):
        out.setdefault("window_id", ctx["window_ids"][0])
    return out


def run_recipe(
    config: Config,
    recipe_name: str,
    rules_name: Optional[str] = None,
    gate: Optional[ApprovalGate] = None,
    on_step: Optional[Callable[[Step], None]] = None,
) -> RunResult:
    """Execute a recipe end-to-end (gated + logged, no LLM)."""
    path = recipes_dir(config) / f"{recipe_name}.yaml"
    if not path.exists():
        available = ", ".join(p.stem for p in recipes_dir(config).glob("*.yaml")) or "(none)"
        raise FileNotFoundError(f"Unknown recipe '{recipe_name}'. Available: {available}")

    recipe = load_recipe(path)
    ruleset = load_ruleset(config, rules_name)
    gate = gate or ApprovalGate(ruleset, confirmer=lambda _: True)

    binary = driver_bin()
    if not binary:
        raise RuntimeError("cua-driver not found. Install the Cua Driver first.")
    if not ensure_daemon(binary):
        raise RuntimeError("Cua Driver daemon is not running. Try: open -n -g -a CuaDriver --args serve")

    logger = RunLogger(config)
    task_label = f"RECIPE: {recipe.name} — {recipe.description}"
    run_id = logger.start_run(task_label, "recipe", ruleset.name, meta={"recipe": recipe.name})
    logger.log_event("recipe_start", {"name": recipe.name, "steps": len(recipe.steps)})

    launch_ctx: dict = {"pid": None, "window_ids": []}
    steps_out: List[Step] = []
    summary = ""

    try:
        for i, rstep in enumerate(recipe.steps, start=1):
            if rstep.wait_seconds > 0 or rstep.tool == "wait":
                wait = rstep.wait_seconds or 1.0
                time.sleep(wait)
                step = Step(i, f"wait {wait}s", Decision.ALLOW.value, "recipe", True, f"slept {wait}s")
                steps_out.append(step)
                if on_step:
                    on_step(step)
                logger.log_event("recipe_wait", {"seconds": wait})
                continue

            tool = rstep.tool
            args = _apply_launch_context(rstep.args, launch_ctx) if rstep.use_launch_context else dict(rstep.args)
            label = rstep.description or f"{tool} {args}"

            action = Action(tool=tool, description=label, args=args)
            verdict = gate.check(action)
            logger.log_event("action_gate", {"index": i, "tool": tool, "args": args,
                                              "decision": verdict.decision.value})
            if verdict.decision is not Decision.ALLOW:
                step = Step(i, label, verdict.decision.value, verdict.reason, False)
                steps_out.append(step)
                if on_step:
                    on_step(step)
                summary = f"stopped at step {i}: {verdict.reason}"
                break

            ok, result = driver_call(binary, tool, args, config.step_timeout)
            step = Step(i, label, Decision.ALLOW.value, "executed", True, result[:800])
            steps_out.append(step)
            if on_step:
                on_step(step)
            logger.log_event("recipe_step", {"index": i, "tool": tool, "ok": ok, "output": result[:3000]})

            if tool == "launch_app" and ok:
                launch_ctx = _parse_launch_context(result)
                logger.log_event("launch_context", launch_ctx)
                if config.auto_snapshot_after_launch and launch_ctx.get("window_ids"):
                    s_ok, snap = _auto_snapshot(binary, launch_ctx, config, logger, run_id or "", i)
                    if s_ok:
                        launch_ctx["snapshot_done"] = True
                        logger.log_event("recipe_auto_snapshot", {"chars": len(snap)})

            if tool == "get_window_state" and ok and not _valid_window_id(args.get("window_id")):
                summary = f"step {i} get_window_state missing window_id; {_launch_context_hint(launch_ctx)}"
                break

            if not ok:
                summary = f"step {i} failed ({tool}): {result[:200]}"
                break
        else:
            summary = f"Recipe '{recipe.name}' completed ({len(steps_out)} steps)."

        status = "failed" if summary.startswith(("stopped", "step ")) else "completed"
        logger.finish_run(status, summary)
        return RunResult(run_id or "", status, summary, "recipe", ruleset.name, steps_out)
    except Exception as exc:
        logger.log_event("recipe_error", {"message": str(exc)})
        logger.finish_run("failed", str(exc))
        raise
    finally:
        logger.close()
