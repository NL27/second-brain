"""Per-task rules + the guardrail / approval gate.

Rules live as YAML files in the ``rules/`` directory. Each task loads a rule
set (by default the one named in config) which controls:
  - extra system instructions injected into the agent prompt
  - allow / deny lists for shell commands, paths, and apps
  - which actions are treated as "destructive" and therefore gated

The :class:`ApprovalGate` turns an intended action into a decision:
ALLOW, CONFIRM (ask the human), or DENY.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable, Dict, List, Optional

import yaml

from .config import Config


class Decision(str, Enum):
    ALLOW = "allow"
    CONFIRM = "confirm"
    DENY = "deny"


@dataclass
class RuleSet:
    name: str
    system_prompt: str = ""
    require_approval_for_destructive: bool = True
    # Substrings/regex that mark an action as destructive (case-insensitive).
    destructive_patterns: List[str] = field(default_factory=list)
    # Hard blocks: if an action matches, it is denied outright.
    deny_patterns: List[str] = field(default_factory=list)
    # Filesystem fences.
    path_deny: List[str] = field(default_factory=list)
    path_allow: List[str] = field(default_factory=list)
    # Application fences (by name or bundle id).
    app_deny: List[str] = field(default_factory=list)
    app_allow: List[str] = field(default_factory=list)

    def _compiled(self, patterns: List[str]) -> List[re.Pattern]:
        return [re.compile(p, re.IGNORECASE) for p in patterns]

    def is_denied(self, blob: str) -> bool:
        return any(p.search(blob) for p in self._compiled(self.deny_patterns))

    def is_destructive(self, blob: str) -> bool:
        return any(p.search(blob) for p in self._compiled(self.destructive_patterns))


@dataclass
class Action:
    """An intended action the agent wants to take, before execution."""

    tool: str
    description: str = ""
    args: Dict[str, object] = field(default_factory=dict)

    def blob(self) -> str:
        """Flatten the action into a searchable string for rule matching."""
        parts = [self.tool, self.description]
        for value in self.args.values():
            parts.append(str(value))
        return " ".join(parts)


@dataclass
class GateResult:
    decision: Decision
    reason: str
    action: Action


def load_ruleset(config: Config, name: Optional[str] = None) -> RuleSet:
    """Load a rule set by name, layered on top of ``default`` when distinct."""
    name = name or config.default_rules
    default = _load_one(config.rules_dir, "default")
    if name == "default":
        merged = default
    else:
        named = _load_one(config.rules_dir, name)
        merged = _merge(default, named)

    # Config-level switch can force approval on regardless of file contents.
    if config.require_approval_for_destructive:
        merged.require_approval_for_destructive = True
    return merged


def _load_one(rules_dir: Path, name: str) -> RuleSet:
    path = rules_dir / f"{name}.yaml"
    if not path.exists():
        return RuleSet(name=name)
    with open(path, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    return RuleSet(
        name=data.get("name", name),
        system_prompt=data.get("system_prompt", ""),
        require_approval_for_destructive=bool(
            data.get("require_approval_for_destructive", True)
        ),
        destructive_patterns=list(data.get("destructive_patterns", [])),
        deny_patterns=list(data.get("deny_patterns", [])),
        path_deny=list(data.get("path_deny", [])),
        path_allow=list(data.get("path_allow", [])),
        app_deny=list(data.get("app_deny", [])),
        app_allow=list(data.get("app_allow", [])),
    )


def _merge(base: RuleSet, over: RuleSet) -> RuleSet:
    return RuleSet(
        name=over.name,
        system_prompt="\n".join(p for p in [base.system_prompt, over.system_prompt] if p),
        require_approval_for_destructive=over.require_approval_for_destructive,
        destructive_patterns=base.destructive_patterns + over.destructive_patterns,
        deny_patterns=base.deny_patterns + over.deny_patterns,
        path_deny=base.path_deny + over.path_deny,
        path_allow=base.path_allow + over.path_allow,
        app_deny=base.app_deny + over.app_deny,
        app_allow=base.app_allow + over.app_allow,
    )


# A confirmer takes a GateResult and returns True to proceed, False to abort.
Confirmer = Callable[[GateResult], bool]


def always_deny(_: GateResult) -> bool:
    return False


class ApprovalGate:
    """Evaluates intended actions against a rule set and a human confirmer."""

    def __init__(self, ruleset: RuleSet, confirmer: Optional[Confirmer] = None):
        self.ruleset = ruleset
        self.confirmer = confirmer or always_deny

    def evaluate(self, action: Action) -> GateResult:
        blob = action.blob()
        if self.ruleset.is_denied(blob):
            return GateResult(Decision.DENY, "matched a deny rule", action)
        if self.ruleset.is_destructive(blob):
            if self.ruleset.require_approval_for_destructive:
                return GateResult(Decision.CONFIRM, "action is destructive", action)
            return GateResult(Decision.ALLOW, "destructive but approval disabled", action)
        return GateResult(Decision.ALLOW, "no rule triggered", action)

    def check(self, action: Action) -> GateResult:
        """Full gate: evaluate, then ask the human if confirmation is required.

        Returns a GateResult whose decision is resolved to ALLOW or DENY.
        """
        result = self.evaluate(action)
        if result.decision is Decision.CONFIRM:
            approved = self.confirmer(result)
            return GateResult(
                Decision.ALLOW if approved else Decision.DENY,
                "approved by human" if approved else "rejected by human",
                action,
            )
        return result
