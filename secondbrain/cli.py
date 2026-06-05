"""The ``brain`` command-line interface.

Commands:
  brain doctor              Check environment (Python, deps, Ollama, keys).
  brain models              List the model registry + availability.
  brain run-task "..."      Run one task (plan-only unless backend=cua).
  brain chat                Interactive text chat / task loop.
  brain eval "..."          Compare models on the same task.
  brain runs                Show recent versioned runs.
  brain ui                  Launch the optional Gradio chat UI.
"""

from __future__ import annotations

import os
import sys
from typing import Optional

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from .config import load_config
from .core import Agent
from .eval import run_eval
from .logging import list_runs
from .models import ModelRouter, provider_available
from .rules import Decision, GateResult

app = typer.Typer(add_completion=False, help="Second Brain - personal computer-control agent.")
console = Console()


def _make_confirmer(auto_yes: bool):
    def confirmer(result: GateResult) -> bool:
        if auto_yes:
            console.print(f"[yellow]auto-approving[/] destructive action: {result.action.description}")
            return True
        console.print(Panel(
            f"[bold]{result.action.description}[/]\n\n[dim]reason:[/] {result.reason}",
            title="[red]Approval required[/]", border_style="red",
        ))
        return typer.confirm("Proceed with this action?", default=False)
    return confirmer


@app.command()
def check(
    json_out: bool = typer.Option(False, "--json", help="Machine-readable output."),
    install: bool = typer.Option(False, "--install", help="Attempt safe installs."),
):
    """Portable system check + install recommendations (works pre-install)."""
    from pathlib import Path

    script = Path(__file__).resolve().parents[1] / "scripts" / "check_system.py"
    if not script.exists():
        console.print(f"[red]check_system.py not found at {script}[/]")
        raise typer.Exit(code=1)
    argv = [sys.executable, str(script)]
    if json_out:
        argv.append("--json")
    if install:
        argv.append("--install")
    raise typer.Exit(code=__import__("subprocess").call(argv))


@app.command()
def doctor():
    """Check that the environment is ready."""
    cfg = load_config()
    table = Table(title="Second Brain - environment check")
    table.add_column("Check")
    table.add_column("Status")

    py_ok = sys.version_info >= (3, 9)
    table.add_row("Python >= 3.9", "[green]ok[/]" if py_ok else "[red]too old[/]")
    table.add_row("Python >= 3.11 (optional cua-agent SDK only)",
                  "[green]ok[/]" if sys.version_info >= (3, 11) else "[yellow]n/a (driver host control works on 3.9+)[/]")

    for mod in ("yaml", "dotenv", "litellm"):
        try:
            __import__(mod)
            table.add_row(f"import {mod}", "[green]ok[/]")
        except Exception:
            table.add_row(f"import {mod}", "[yellow]missing[/]")

    # Host control = the Cua Driver (drives your real Mac). This is what
    # backend=driver uses.
    try:
        from .control_driver import driver_bin, daemon_running
        b = driver_bin()
        if b:
            running = daemon_running(b)
            table.add_row("Cua Driver (host control)",
                          "[green]ok[/]" + ("" if running else " [yellow](daemon not running)[/]"))
        else:
            table.add_row("Cua Driver (host control)", "[yellow]not installed[/]")
    except Exception as exc:
        table.add_row("Cua Driver (host control)", f"[yellow]error: {exc}[/]")

    # The cua-agent SDK is the separate sandbox/VM backend (backend=cua).
    try:
        from .control_cua import _import_cua
        _import_cua()
        table.add_row("cua-agent SDK (sandbox)", "[green]ok[/]")
    except Exception:
        table.add_row("cua-agent SDK (sandbox)", "[yellow]not installed[/]")

    table.add_row("Ollama host", os.getenv("OLLAMA_HOST", "http://localhost:11434"))
    table.add_row("backend", cfg.control_backend)
    table.add_row("dry_run", str(cfg.dry_run))
    console.print(table)


@app.command()
def models():
    """List the model registry and whether each is usable right now."""
    cfg = load_config()
    router = ModelRouter(cfg)
    table = Table(title="Model registry")
    table.add_column("Key")
    table.add_column("Model")
    table.add_column("Where")
    table.add_column("Vision")
    table.add_column("Available")
    for key, spec in router.registry().items():
        avail = provider_available(spec.model)
        table.add_row(
            f"[bold]{key}[/]" + (" *" if key == cfg.default_model else ""),
            spec.model,
            spec.location,
            "yes" if spec.vision else "no",
            "[green]yes[/]" if avail else "[yellow]needs key[/]",
        )
    console.print(table)
    if not router.has_backend:
        console.print("[yellow]litellm not installed; runs use offline stub plans.[/]")


@app.command(name="run-task")
def run_task(
    task: str = typer.Argument(..., help="What you want done on your Mac."),
    model: Optional[str] = typer.Option(None, "--model", "-m", help="Model key."),
    rules: Optional[str] = typer.Option(None, "--rules", "-r", help="Rule set name."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Auto-approve destructive actions."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Plan + log only; never execute."),
):
    """Run a single task end to end (gated + logged + versioned)."""
    overrides = {"safety": {"dry_run": True}} if dry_run else None
    cfg = load_config(overrides=overrides)
    agent = Agent(cfg, confirmer=_make_confirmer(yes))

    def on_step(step):
        color = {"allow": "green", "confirm": "yellow", "deny": "red"}.get(step.decision, "white")
        console.print(f"  [dim]{step.index:>2}[/] [{color}]{step.decision:<7}[/] {step.description}")

    console.print(Panel(task, title="Task", border_style="cyan"))
    result = agent.run_task(task, model_key=model, rules_name=rules, on_step=on_step)
    console.print(Panel(result.summary, title=f"Result ({result.status})",
                        border_style="green" if result.status == "completed" else "red"))
    console.print(f"[dim]run {result.run_id} - logged & versioned under logs/[/]")


@app.command()
def chat(
    model: Optional[str] = typer.Option(None, "--model", "-m"),
    rules: Optional[str] = typer.Option(None, "--rules", "-r"),
    yes: bool = typer.Option(False, "--yes", "-y"),
):
    """Interactive text loop. Type a task; 'exit' to quit."""
    cfg = load_config()
    agent = Agent(cfg, confirmer=_make_confirmer(yes))
    console.print(Panel("Second Brain chat. Type a task, or 'exit'.", border_style="cyan"))
    while True:
        try:
            msg = console.input("[bold cyan]you[/] > ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if msg.lower() in ("exit", "quit", ":q"):
            break
        if not msg:
            continue

        def on_step(step):
            color = {"allow": "green", "confirm": "yellow", "deny": "red"}.get(step.decision, "white")
            console.print(f"  [{color}]{step.decision:<7}[/] {step.description}")

        result = agent.run_task(msg, model_key=model, rules_name=rules, on_step=on_step)
        console.print(Panel(result.summary, title=result.status, border_style="green"))


@app.command(name="eval")
def eval_cmd(
    task: str = typer.Argument(..., help="Task to evaluate models on."),
    models_csv: str = typer.Option(..., "--models", help="Comma-separated model keys."),
    rules: Optional[str] = typer.Option(None, "--rules", "-r"),
    judge: Optional[str] = typer.Option(None, "--judge", help="Model key to use as judge."),
):
    """Run the same task across several models and rank them."""
    cfg = load_config()
    keys = [k.strip() for k in models_csv.split(",") if k.strip()]
    report = run_eval(cfg, task, keys, rules_name=rules, judge_key=judge)

    table = Table(title=f"Eval: {task}")
    table.add_column("Rank")
    table.add_column("Model key")
    table.add_column("Where")
    table.add_column("OK")
    table.add_column("Latency")
    table.add_column("Cost")
    table.add_column("Steps")
    table.add_column("Auto")
    table.add_column("Judge")
    table.add_column("Overall")
    for i, ev in enumerate(report.ranked(), start=1):
        table.add_row(
            str(i), ev.model_key, ev.location,
            "[green]yes[/]" if ev.ok else "[red]no[/]",
            f"{ev.latency_s:.2f}s",
            f"${ev.cost_usd:.4f}" if ev.cost_usd else "-",
            str(ev.num_steps),
            f"{ev.auto_score:.1f}",
            f"{ev.judge_score:.1f}" if ev.judge_score is not None else "-",
            f"[bold]{ev.overall:.1f}[/]",
        )
    console.print(table)
    w = report.winner()
    if w:
        console.print(f"[green]Best for this task:[/] [bold]{w.model_key}[/] ({w.model})")


@app.command()
def runs(limit: int = typer.Option(15, "--limit", "-n")):
    """Show recent runs from the versioned index."""
    cfg = load_config()
    rows = list_runs(cfg, limit=limit)
    table = Table(title="Recent runs")
    table.add_column("Run")
    table.add_column("Started")
    table.add_column("Model")
    table.add_column("Status")
    table.add_column("Events")
    table.add_column("Task")
    for r in rows:
        table.add_row(r["run_id"], (r["started_at"] or "")[:19], r["model_key"] or "",
                      r["status"] or "", str(r["num_events"]), (r["task"] or "")[:50])
    console.print(table)


@app.command()
def ui():
    """Launch the optional Gradio chat UI."""
    try:
        from .webui import launch
    except Exception as exc:
        console.print(f"[red]UI unavailable:[/] {exc}")
        console.print("Install with: pip install 'secondbrain[ui]'")
        raise typer.Exit(code=1)
    launch()


def main():
    app()


if __name__ == "__main__":
    main()
