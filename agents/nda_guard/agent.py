from __future__ import annotations

from demo import DEFAULT_LIVE_MODEL, build_agent

# ADK Web discovers this symbol. The implementation lives in demo.py so the
# batch demo and browser chat exercise the same agent definition.
root_agent = build_agent(model_name=DEFAULT_LIVE_MODEL)
