"""Second Brain - a personal computer-control agent core.

Phase 1: an integrate-first, host-controlling, multi-LLM, fully-logged agent.
The public surface intentionally stays small and stable - this is the
"protected core" that later phases (knowledge base, scheduling, messaging,
voice) plug into via MCP.
"""

from __future__ import annotations

__version__ = "0.1.0"

from .config import Config, load_config

__all__ = ["Config", "load_config", "__version__"]
