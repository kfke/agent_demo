"""Tool-call contracts: validation, bounded repair, fallback.

Answers question 3. The model is an untrusted producer of tool calls. It
will emit a missing argument, a string where an int belongs, a tool that
was renamed last sprint, or an argument that no longer exists.

None of those may raise out of the turn. Each becomes a structured result
the model can act on, and the number of times it may act on it is bounded.

Zero google.adk imports, same reason as policy.py.
"""

from __future__ import annotations

import dataclasses
from typing import Any, Optional

# =============================================================
# Schema
# =============================================================


@dataclasses.dataclass(frozen=True)
class ToolSchema:
    """The contract the model must satisfy to call a tool.

    `version` is what makes schema drift detectable rather than mysterious:
    the deployed agent pins a version, and a mismatch is an alert, not a
    confusing 500 at 3am.
    """

    name: str
    version: int
    required: dict[str, type]
    optional: dict[str, type] = dataclasses.field(default_factory=dict)

    def validate(self, args: dict[str, Any]) -> list[str]:
        """Returns a list of problems. Empty list means the call is well-formed."""
        problems: list[str] = []
        known = {**self.required, **self.optional}

        for arg, want in self.required.items():
            if arg not in args:
                problems.append(f"missing required argument '{arg}' ({want.__name__})")

        for arg, value in args.items():
            if arg not in known:
                problems.append(f"unknown argument '{arg}' is not in {self.name} v{self.version}")
                continue
            want = known[arg]
            # bool is a subclass of int in Python; an agent passing True for
            # a count is a real bug, so reject it explicitly.
            if want is int and isinstance(value, bool):
                problems.append(f"argument '{arg}' must be int, got bool")
            elif not isinstance(value, want):
                problems.append(
                    f"argument '{arg}' must be {want.__name__}, got {type(value).__name__}"
                )
        return problems


class SchemaRegistry:
    """What the runtime believes the tools look like.

    Drift detection: the agent bundle records the versions it was tested
    against. On boot, compare. A tool that moved from v1 to v2 fails the
    health check BEFORE it fails a user.
    """

    def __init__(self, schemas: list[ToolSchema]) -> None:
        self._by_name = {s.name: s for s in schemas}

    def get(self, name: str) -> Optional[ToolSchema]:
        return self._by_name.get(name)

    def drift_against(self, pinned: dict[str, int]) -> list[str]:
        """Compare live schemas to the versions this build was tested on."""
        drift: list[str] = []
        for name, want_version in pinned.items():
            live = self._by_name.get(name)
            if live is None:
                drift.append(f"tool '{name}' pinned at v{want_version} has disappeared")
            elif live.version != want_version:
                drift.append(
                    f"tool '{name}' pinned at v{want_version}, runtime has v{live.version}"
                )
        for name in self._by_name:
            if name not in pinned:
                drift.append(f"tool '{name}' appeared and was never pinned")
        return drift


# =============================================================
# Bounded repair
# =============================================================


@dataclasses.dataclass
class RepairOutcome:
    status: str  # "valid" | "repaired" | "fallback"
    args: Optional[dict[str, Any]]
    attempts: int
    problems: list[str] = dataclasses.field(default_factory=list)


class RepairLoop:
    """Hands validation errors back to the model, at most `max_repairs` times.

    Unbounded repair is the classic outage: a model that cannot satisfy a
    schema will cheerfully burn your entire token budget failing. The bound
    is the guardrail; the fallback is what the user gets instead of a stall.
    """

    def __init__(self, registry: SchemaRegistry, max_repairs: int = 1) -> None:
        self.registry = registry
        self.max_repairs = max_repairs

    def run(
        self,
        tool_name: str,
        proposals: list[dict[str, Any]],
    ) -> RepairOutcome:
        """`proposals` is what the model produces on each successive try."""
        schema = self.registry.get(tool_name)
        if schema is None:
            return RepairOutcome("fallback", None, 0,
                                 [f"tool '{tool_name}' is not registered"])

        problems: list[str] = []
        for i, args in enumerate(proposals[: self.max_repairs + 1]):
            problems = schema.validate(args)
            if not problems:
                return RepairOutcome("valid" if i == 0 else "repaired", args, i + 1)

        return RepairOutcome("fallback", None, min(len(proposals), self.max_repairs + 1),
                             problems)

    @staticmethod
    def repair_prompt(tool_name: str, problems: list[str]) -> str:
        """What goes back to the model. Specific beats 'invalid input'."""
        bullets = "\n".join(f"- {p}" for p in problems)
        return (
            f"Your call to {tool_name} was rejected before it ran:\n{bullets}\n"
            "Emit the call again with those fixed, or say you cannot."
        )
