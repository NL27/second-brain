"""Model registry + multi-LLM router.

Thin wrapper over liteLLM so the same code path reaches local (Ollama) and
cloud providers. If liteLLM is not installed, the router still loads and
reports availability, but completion calls raise a clear error.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Dict, List, Optional

from .config import Config, ModelSpec

try:
    import litellm  # type: ignore

    _HAS_LITELLM = True
except Exception:  # pragma: no cover - optional dependency
    litellm = None  # type: ignore
    _HAS_LITELLM = False


# Maps a liteLLM provider prefix -> the env var that must be set to use it.
# Local providers (ollama) need no key.
_PROVIDER_ENV = {
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "groq": "GROQ_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "dashscope": "DASHSCOPE_API_KEY",
    "cursor": "CURSOR_API_KEY",
}


@dataclass
class Completion:
    """Normalized result of a single model call."""

    model_key: str
    model: str
    text: str
    latency_s: float
    prompt_tokens: Optional[int] = None
    completion_tokens: Optional[int] = None
    cost_usd: Optional[float] = None
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.error is None


def _provider_of(model: str) -> str:
    return model.split("/", 1)[0] if "/" in model else model


def provider_available(model: str) -> bool:
    """Whether the prerequisites (key or local server) for a model are present."""
    provider = _provider_of(model)
    if provider == "ollama":
        return True  # Availability is checked at call time against the host.
    env_var = _PROVIDER_ENV.get(provider)
    if env_var is None:
        return True  # Unknown provider: assume the user knows what they configured.
    return bool(os.getenv(env_var))


class ModelRouter:
    """Routes completion requests to local or cloud models via liteLLM."""

    def __init__(self, config: Config):
        self.config = config
        self._registry: Dict[str, ModelSpec] = config.models()

    @property
    def has_backend(self) -> bool:
        return _HAS_LITELLM

    def registry(self) -> Dict[str, ModelSpec]:
        return dict(self._registry)

    def available(self) -> Dict[str, bool]:
        return {key: provider_available(spec.model) for key, spec in self._registry.items()}

    def complete(
        self,
        model_key: str,
        messages: List[Dict[str, str]],
        temperature: float = 0.2,
        max_tokens: Optional[int] = None,
    ) -> Completion:
        """Call a single registered model and normalize the response."""
        spec = self.config.model(model_key)

        # Cursor SDK provider: use the Cursor subscription, bypassing liteLLM.
        if _provider_of(spec.model) == "cursor":
            from .provider_cursor import cursor_complete

            model_id = spec.model.split("/", 1)[1] if "/" in spec.model else "auto"
            start = time.time()
            text, err = cursor_complete(model_id, messages)
            return Completion(
                model_key=model_key,
                model=spec.model,
                text=text or "",
                latency_s=round(time.time() - start, 3),
                error=err,
            )

        if not _HAS_LITELLM:
            return Completion(
                model_key=model_key,
                model=spec.model,
                text="",
                latency_s=0.0,
                error="litellm is not installed (pip install 'secondbrain[llm]').",
            )

        kwargs: Dict[str, object] = {
            "model": spec.model,
            "messages": messages,
            "temperature": temperature,
        }
        if max_tokens:
            kwargs["max_tokens"] = max_tokens
        if _provider_of(spec.model) == "ollama":
            kwargs["api_base"] = os.getenv("OLLAMA_HOST", "http://localhost:11434")

        start = time.time()
        try:
            resp = litellm.completion(**kwargs)  # type: ignore[arg-type]
        except Exception as exc:  # network, auth, missing model, etc.
            return Completion(
                model_key=model_key,
                model=spec.model,
                text="",
                latency_s=round(time.time() - start, 3),
                error=str(exc),
            )

        latency = round(time.time() - start, 3)
        text = ""
        try:
            text = resp["choices"][0]["message"]["content"] or ""
        except Exception:
            text = str(resp)

        usage = getattr(resp, "usage", None) or {}
        prompt_tokens = getattr(usage, "prompt_tokens", None) or usage.get("prompt_tokens") if usage else None
        completion_tokens = (
            getattr(usage, "completion_tokens", None) or usage.get("completion_tokens") if usage else None
        )

        cost = None
        try:
            cost = litellm.completion_cost(completion_response=resp)  # type: ignore[attr-defined]
        except Exception:
            cost = None

        return Completion(
            model_key=model_key,
            model=spec.model,
            text=text,
            latency_s=latency,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cost_usd=cost,
        )
