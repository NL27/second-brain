"""Optional Gradio chat UI. Install with: pip install 'secondbrain[ui]'."""

from __future__ import annotations

from .config import load_config
from .core import Agent


def launch(server_port: int = 7860):
    import gradio as gr  # imported lazily; optional dependency

    cfg = load_config()
    # In the UI we auto-approve nothing: destructive steps surface in the log
    # panel and (in cua mode) are blocked unless approval is wired to the UI.
    agent = Agent(cfg)

    def respond(message, history):
        result = agent.run_task(message)
        lines = [f"**{result.status}** - {result.summary}", ""]
        for s in result.steps:
            mark = {"allow": "✅", "confirm": "⚠️", "deny": "⛔"}.get(s.decision, "•")
            lines.append(f"{mark} {s.description}")
        lines.append(f"\n_run {result.run_id} - logged & versioned_")
        return "\n".join(lines)

    demo = gr.ChatInterface(
        fn=respond,
        title="Second Brain",
        description="Personal computer-control agent (plan-only unless cua backend is enabled).",
    )
    demo.launch(server_port=server_port)
