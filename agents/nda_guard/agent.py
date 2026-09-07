from __future__ import annotations

import os

from google.adk.agents import LlmAgent
from google.adk.models.lite_llm import LiteLlm

from ndaguard.tools import ALL_TOOLS

MODEL = os.environ.get("OPENAI_MODEL", "openai/gpt-4.1-mini")

root_agent = LlmAgent(
    name="nda_guard",
    model=LiteLlm(model=MODEL),
    instruction=(
        "You are an NDA review agent for a legal team. Use tools for document "
        "search, clause reads, redlining, and signature requests. The session "
        "defaults to role=legal_counsel and tenant=tenant-eu unless changed in "
        "ADK Web session state. When a tool is blocked by policy, explain the "
        "rule and do not invent a successful action."
    ),
    tools=ALL_TOOLS,
)
