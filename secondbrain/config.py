"""Configuration loading and the typed config object.

Precedence (highest first): CLI flags -> environment variables ->
./config.yaml (project root) -> packaged default_config.yaml.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional

import yaml

try:
    from dotenv import load_dotenv as _load_dotenv
except Exception:  # pragma: no cover - optional dependency
    _load_dotenv = None  # type: ignore


def _load_env_file(root: Path) -> Optional[Path]:
    """Load ``.env`` from the project root (not only the current working directory)."""
    if _load_dotenv is None:
        return None
    env_path = root / ".env"
    if env_path.exists():
        _load_dotenv(dotenv_path=env_path, override=False)
        return env_path
    # Fallback: walk up from cwd (helps if brain is run from a subdirectory).
    _load_dotenv(override=False)
    return None

_PACKAGE_DIR = Path(__file__).resolve().parent
_DEFAULT_CONFIG_PATH = _PACKAGE_DIR / "default_config.yaml"


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    """Recursively merge ``override`` into ``base`` (returns a new dict)."""
    out = dict(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


@dataclass
class ModelSpec:
    """A single entry in the model registry."""

    key: str
    model: str
    location: str = "cloud"  # "local" | "cloud"
    vision: bool = False
    notes: str = ""

    @property
    def is_local(self) -> bool:
        return self.location == "local"


@dataclass
class Config:
    """Resolved configuration for a Second Brain session."""

    root: Path
    raw: Dict[str, Any] = field(default_factory=dict)

    # Convenience accessors -------------------------------------------------
    @property
    def logs_dir(self) -> Path:
        return (self.root / self.raw["paths"]["logs_dir"]).resolve()

    @property
    def db_path(self) -> Path:
        return (self.root / self.raw["paths"]["db_path"]).resolve()

    @property
    def rules_dir(self) -> Path:
        return (self.root / self.raw["paths"]["rules_dir"]).resolve()

    @property
    def control_backend(self) -> str:
        return self.raw["control"]["backend"]

    @property
    def max_steps(self) -> int:
        return int(self.raw["control"]["max_steps"])

    @property
    def step_timeout(self) -> int:
        return int(self.raw["control"]["step_timeout"])

    @property
    def default_model(self) -> str:
        return self.raw["default_model"]

    @property
    def dry_run(self) -> bool:
        return bool(self.raw["safety"]["dry_run"])

    @property
    def require_approval_for_destructive(self) -> bool:
        return bool(self.raw["safety"]["require_approval_for_destructive"])

    @property
    def default_rules(self) -> str:
        return self.raw["safety"]["default_rules"]

    def models(self) -> Dict[str, ModelSpec]:
        specs: Dict[str, ModelSpec] = {}
        for key, entry in (self.raw.get("models") or {}).items():
            specs[key] = ModelSpec(
                key=key,
                model=entry["model"],
                location=entry.get("location", "cloud"),
                vision=bool(entry.get("vision", False)),
                notes=entry.get("notes", ""),
            )
        return specs

    def model(self, key: str) -> ModelSpec:
        specs = self.models()
        if key not in specs:
            raise KeyError(
                f"Unknown model '{key}'. Known models: {', '.join(specs) or '(none)'}"
            )
        return specs[key]


def load_config(
    root: Optional[Path] = None,
    overrides: Optional[Dict[str, Any]] = None,
) -> Config:
    """Build a :class:`Config` by merging defaults, project file, env, overrides."""
    root = Path(root or os.getcwd()).resolve()
    _load_env_file(root)

    with open(_DEFAULT_CONFIG_PATH, "r", encoding="utf-8") as fh:
        merged: Dict[str, Any] = yaml.safe_load(fh) or {}

    project_cfg = root / "config.yaml"
    if project_cfg.exists():
        with open(project_cfg, "r", encoding="utf-8") as fh:
            merged = _deep_merge(merged, yaml.safe_load(fh) or {})

    # Environment overrides for the most safety-critical switches.
    if os.getenv("BRAIN_DRY_RUN"):
        merged["safety"]["dry_run"] = os.getenv("BRAIN_DRY_RUN") not in ("0", "false", "")
    if os.getenv("BRAIN_REQUIRE_APPROVAL"):
        merged["safety"]["require_approval_for_destructive"] = os.getenv(
            "BRAIN_REQUIRE_APPROVAL"
        ) not in ("0", "false", "")

    if overrides:
        merged = _deep_merge(merged, overrides)

    return Config(root=root, raw=merged)
