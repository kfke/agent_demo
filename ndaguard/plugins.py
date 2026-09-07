"""The two guardrail layers, as ADK plugins.

Layer 1  ContentScanPlugin  -> before_model / after_model / after_tool
         Probabilistic. Catches content risk. Stands in for Model Armor
         so this demo runs with no GCP project.

Layer 2  PolicyPlugin       -> before_tool
         Deterministic. Catches action risk. This is the one that can
         actually stop damage.
"""

from __future__ import annotations

import json
import re
from typing import Any, Optional

from google.adk.agents.base_agent import BaseAgent
from google.adk.agents.callback_context import CallbackContext
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.plugins.base_plugin import BasePlugin
from google.adk.tools import BaseTool
from google.adk.tools.tool_context import ToolContext
from google.genai import types

from .policy import Decision, PolicyEngine

AUDIT: list[dict[str, Any]] = []


def _audit(**row: Any) -> None:
    AUDIT.append(row)
    print("  AUDIT " + json.dumps(row, separators=(",", ":")))


# =============================================================
# Layer 1 -- content screening (the "Model Armor" slot)
# =============================================================

_INJECTION = re.compile(
    r"ignore (all )?(previous|prior) instructions"
    r"|system note for the assistant"
    r"|you are now authorised",
    re.IGNORECASE,
)


class ContentScanPlugin(BasePlugin):
    """Screens text going into and coming out of the model.

    In production you replace this whole class with:

        from google.adk.integrations.model_armor import (
            ModelArmorConfig, ModelArmorPlugin)

        ModelArmorPlugin(config=ModelArmorConfig(
            prompt_template_name="projects/p/locations/eu/templates/nda-in",
            response_template_name="projects/p/locations/eu/templates/nda-out",
            block_on_screening_failure=True,   # fail closed
        ))

    Note what Google's own plugin hooks: before_model_callback and
    after_model_callback. Only those two. It never sees a tool call.
    """

    def __init__(self, enabled: bool = True) -> None:
        super().__init__(name="content_scan")
        self.enabled = enabled

    async def before_model_callback(
        self, *, callback_context: CallbackContext, llm_request: LlmRequest
    ) -> Optional[LlmResponse]:
        if not self.enabled:
            return None
        text = " ".join(
            p.text or ""
            for c in (llm_request.contents or [])
            for p in (c.parts or [])
        )
        if _INJECTION.search(text):
            _audit(layer="model", hook="before_model", effect="block",
                   detector="injection_pattern")
            return LlmResponse(
                content=types.Content(
                    role="model",
                    parts=[types.Part(text="Blocked: the prompt contains an instruction override.")],
                )
            )
        return None

    async def after_tool_callback(
        self, *, tool: BaseTool, tool_args: dict[str, Any],
        tool_context: ToolContext, result: dict[str, Any],
    ) -> Optional[dict[str, Any]]:
        """Retrieved documents are untrusted input. Scan them too.

        Model Armor's built-in plugin does NOT do this. If your agent reads
        counterparty PDFs, you have to add this hook yourself.
        """
        if not self.enabled:
            return None
        if _INJECTION.search(json.dumps(result)):
            _audit(layer="model", hook="after_tool", effect="redact",
                   tool=tool.name, detector="injection_pattern")
            return {**result, "text": "[content withheld: embedded instruction detected]"}
        return None


# =============================================================
# Layer 2 -- deterministic policy (the orchestration gate)
# =============================================================


class PolicyPlugin(BasePlugin):
    """Evaluates policy.yaml before every tool call."""

    def __init__(self, engine: PolicyEngine | None = None) -> None:
        super().__init__(name="policy_gate")
        self.engine = engine or PolicyEngine()


    async def before_agent_callback(
        self, *, agent: BaseAgent, callback_context: CallbackContext
    ) -> Optional[types.Content]:
        callback_context.state.setdefault("role", "legal_counsel")
        callback_context.state.setdefault("tenant", "tenant-eu")
        callback_context.state.setdefault("approvals", [])
        return None

    async def before_tool_callback(
        self, *, tool: BaseTool, tool_args: dict[str, Any], tool_context: ToolContext
    ) -> Optional[dict[str, Any]]:
        state = tool_context.state
        decision: Decision = self.engine.evaluate(
            role=state.get("role", "anonymous"),
            tool_name=tool.name,
            args=tool_args,
            session={"tenant": state.get("tenant")},
        )

        _audit(layer="orchestration", hook="before_tool", tool=tool.name,
               role=state.get("role"), effect=decision.effect,
               rule=decision.rule_id, blast_radius=decision.blast_radius,
               policy_version=self.engine.version)

        if decision.effect == "deny":
            # Returning a dict skips the tool. The model sees this text
            # and must explain the refusal to the user.
            return {"error": "policy_denied", "rule": decision.rule_id,
                    "reason": decision.reason}

        if decision.effect == "needs_approval":
            if tool_args.get("doc_id") in state.get("approvals", []):
                return None  # a human already confirmed this one
            return {"error": "approval_required", "rule": decision.rule_id,
                    "reason": decision.reason}

        return None
