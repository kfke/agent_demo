"""A model stand-in that replays a fixed script.

Guardrails must be deterministic to be testable. Swapping the model for a
script is how you test them in CI without paying for tokens or depending
on the network. The Runner, the plugin dispatch and the tool execution are
all the real thing -- only the token generator is faked.
"""

from __future__ import annotations

from typing import Any, AsyncGenerator

from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.genai import types


class ScriptedLlm(BaseLlm):
    """Yields one scripted turn per call.

    script entries:
      {"call": "tool_name", "args": {...}}   -> emits a function call
      {"text": "..."}                        -> emits plain text
    """

    model: str = "scripted-llm"
    script: list[dict[str, Any]] = []
    cursor: int = 0

    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse, None]:
        if self.cursor >= len(self.script):
            yield LlmResponse(
                content=types.Content(role="model", parts=[types.Part(text="(script exhausted)")])
            )
            return

        turn = self.script[self.cursor]
        self.cursor += 1

        if "call" in turn:
            part = types.Part(
                function_call=types.FunctionCall(
                    name=turn["call"], args=turn.get("args", {})
                )
            )
        else:
            part = types.Part(text=turn["text"])

        yield LlmResponse(content=types.Content(role="model", parts=[part]))
