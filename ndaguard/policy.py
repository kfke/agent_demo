"""Deterministic policy engine.

Deliberately has zero imports from google.adk. Policy must be testable
without booting an agent, and portable if you swap frameworks.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any

import yaml

POLICY_PATH = Path(__file__).resolve().parent.parent / "policy.yaml"


@dataclasses.dataclass(frozen=True)
class Decision:
    """Every decision names the rule that made it. That is the audit trail."""

    effect: str  # "allow" | "deny" | "needs_approval"
    rule_id: str
    reason: str
    blast_radius: str = "unknown"

    @property
    def allowed(self) -> bool:
        return self.effect == "allow"


class PolicyEngine:
    def __init__(self, path: Path | str = POLICY_PATH):
        self.doc = yaml.safe_load(Path(path).read_text())
        self.version = self.doc["version"]

    # -- main entry point -------------------------------------
    def evaluate(
        self,
        *,
        role: str,
        tool_name: str,
        args: dict[str, Any],
        session: dict[str, Any],
    ) -> Decision:
        tools = self.doc.get("tools", {})
        spec = tools.get(tool_name)

        # 1. Unknown tool -> deny. Default-deny, never default-allow.
        if spec is None:
            return Decision("deny", "R-000", f"tool '{tool_name}' is not registered in policy")

        radius = spec.get("blast_radius", "unknown")

        # 2. Hard denies win over everything.
        if spec.get("deny_always"):
            return Decision("deny", "R-001", f"tool '{tool_name}' is disabled for all roles", radius)

        # 3. Role must own the tool.
        allowed_tools = self.doc.get("roles", {}).get(role, {}).get("tools", [])
        if tool_name not in allowed_tools:
            return Decision(
                "deny", "R-002",
                f"role '{role}' is not permitted to call '{tool_name}'",
                radius,
            )

        # 4. Argument constraints.
        for c in self.doc.get("constraints", []):
            if c["tool"] != tool_name:
                continue
            value = args.get(c["arg"])
            if value is None:
                continue

            if "allow_values" in c and value not in c["allow_values"]:
                return Decision("deny", c["id"], f"{c['reason']} (got '{value}')", radius)

            if "must_equal_session" in c:
                expected = session.get(c["must_equal_session"])
                if value != expected:
                    return Decision(
                        "deny", c["id"],
                        f"{c['reason']} (got '{value}', session is '{expected}')",
                        radius,
                    )

            if "max_value" in c:
                try:
                    if float(value) > float(c["max_value"]):
                        return Decision("deny", c["id"], f"{c['reason']} (got {value})", radius)
                except (TypeError, ValueError):
                    return Decision("deny", c["id"], f"{c['arg']} is not numeric", radius)

        # 5. Approval gate for irreversible or outbound actions.
        if spec.get("requires_approval"):
            return Decision(
                "needs_approval", "R-010",
                f"'{tool_name}' has blast radius '{radius}' and needs human confirmation",
                radius,
            )

        return Decision("allow", "R-ALLOW", "matched no denying rule", radius)
