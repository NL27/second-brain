"""Multi-model evaluation harness.

Run the SAME task across several models (local vs cloud) and compare them so
you can pick the best model for a given kind of task. For safety, evaluation
runs in plan-only mode by default (models propose plans; nothing is executed),
capturing latency, cost, plan quality signals, and an optional LLM-as-judge
score. Everything is logged + versioned like any other run.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Dict, List, Optional

from .config import Config
from .core import Agent
from .logging import RunLogger
from .models import ModelRouter
from .rules import Action, load_ruleset


@dataclass
class ModelEval:
    model_key: str
    model: str
    location: str
    ok: bool
    latency_s: float
    cost_usd: Optional[float]
    num_steps: int
    num_gated: int
    text: str
    error: Optional[str] = None
    auto_score: float = 0.0
    judge_score: Optional[float] = None
    judge_notes: str = ""

    @property
    def overall(self) -> float:
        if self.judge_score is not None:
            return round(0.5 * self.auto_score + 0.5 * self.judge_score, 2)
        return self.auto_score


@dataclass
class EvalReport:
    task: str
    rules: str
    results: List[ModelEval] = field(default_factory=list)

    def ranked(self) -> List[ModelEval]:
        return sorted(self.results, key=lambda r: (r.ok, r.overall), reverse=True)

    def winner(self) -> Optional[ModelEval]:
        ranked = [r for r in self.ranked() if r.ok]
        return ranked[0] if ranked else None


def _auto_score(ev: ModelEval) -> float:
    """Heuristic 0-10 score from cheap signals (no judge required)."""
    if not ev.ok:
        return 0.0
    score = 5.0
    # Reward a reasonable number of concrete steps (3-12 is a sweet spot).
    if 3 <= ev.num_steps <= 12:
        score += 2.0
    elif ev.num_steps > 0:
        score += 1.0
    # Reward speed (sub-10s gets the bonus, scaled down after).
    if ev.latency_s <= 10:
        score += 1.5
    elif ev.latency_s <= 30:
        score += 0.5
    # Mild penalty if a huge fraction of steps are flagged destructive
    # (often a sign of an unsafe or sloppy plan).
    if ev.num_steps and ev.num_gated / ev.num_steps > 0.5:
        score -= 1.0
    return round(max(0.0, min(10.0, score)), 2)


def _judge(router: ModelRouter, judge_key: str, task: str, text: str) -> tuple:
    """Use an LLM as a judge. Returns (score 0-10 or None, notes)."""
    messages = [
        {"role": "system", "content": "You are a strict evaluator of task plans. "
         "Rate the plan from 0 to 10 for correctness, safety, and clarity. "
         "Respond as: SCORE: <number>\\nNOTES: <one sentence>."},
        {"role": "user", "content": f"Task:\n{task}\n\nPlan:\n{text}"},
    ]
    c = router.complete(judge_key, messages)
    if not c.ok:
        return None, f"judge unavailable: {c.error}"
    score = None
    notes = ""
    for line in c.text.splitlines():
        low = line.lower().strip()
        if low.startswith("score:"):
            try:
                score = float(line.split(":", 1)[1].strip().split()[0])
            except Exception:
                score = None
        elif low.startswith("notes:"):
            notes = line.split(":", 1)[1].strip()
    return (max(0.0, min(10.0, score)) if score is not None else None), notes


def run_eval(
    config: Config,
    task: str,
    model_keys: List[str],
    rules_name: Optional[str] = None,
    judge_key: Optional[str] = None,
) -> EvalReport:
    ruleset = load_ruleset(config, rules_name)
    router = ModelRouter(config)
    agent = Agent(config)

    logger = RunLogger(config)
    logger.start_run(f"EVAL: {task}", "+".join(model_keys), ruleset.name,
                     meta={"eval": True, "judge": judge_key})

    report = EvalReport(task=task, rules=ruleset.name)
    system = agent._system_prompt(ruleset)  # reuse the same framing as real runs

    for key in model_keys:
        spec = config.model(key)
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": f"Task: {task}\n\nProduce a concise, numbered, "
             "step-by-step plan. One action per line. Do not execute anything."},
        ]
        c = router.complete(key, messages)
        steps = Agent._parse_plan(c.text) if c.ok else []
        num_gated = 0
        for line in steps:
            verdict_blob = Action(tool="plan_step", description=line).blob()
            if ruleset.is_denied(verdict_blob) or ruleset.is_destructive(verdict_blob):
                num_gated += 1

        ev = ModelEval(
            model_key=key,
            model=spec.model,
            location=spec.location,
            ok=c.ok,
            latency_s=c.latency_s,
            cost_usd=c.cost_usd,
            num_steps=len(steps),
            num_gated=num_gated,
            text=c.text,
            error=c.error,
        )
        ev.auto_score = _auto_score(ev)
        if judge_key and c.ok:
            ev.judge_score, ev.judge_notes = _judge(router, judge_key, task, c.text)

        report.results.append(ev)
        logger.log_event("eval_model", {**asdict(ev), "overall": ev.overall,
                                        "cost_usd": ev.cost_usd})

    winner = report.winner()
    summary = (
        f"Evaluated {len(model_keys)} models. "
        f"Winner: {winner.model_key} (overall {winner.overall})" if winner
        else f"Evaluated {len(model_keys)} models; no model succeeded."
    )
    logger.log_event("eval_summary", {"summary": summary,
                                      "ranking": [r.model_key for r in report.ranked()]})
    logger.finish_run("completed", summary)
    logger.close()
    return report
