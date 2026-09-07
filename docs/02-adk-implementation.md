# 02 — Implementing both layers in Google ADK

Everything here was verified against `google-adk==2.8.0` by installing the
package and reading the source, not from blog posts. Re-verify after an
upgrade; these APIs have moved before.

## The hook map

ADK gives you a `BasePlugin` registered once on the `Runner`. It applies to
every agent and sub-agent under that Runner.

| Hook | Fires | Layer |
|---|---|---|
| `before_model_callback` | every LLM call | model / content |
| `after_model_callback` | every LLM response | model / content |
| `before_tool_callback` | every tool call | orchestration / action |
| `after_tool_callback` | every tool result | data / content |

Full hook list on `BasePlugin` in 2.8.0:

```
before_run_callback        after_run_callback       on_run_error_callback
on_user_message_callback
before_agent_callback      after_agent_callback     on_agent_error_callback
before_model_callback      after_model_callback     on_model_error_callback
before_tool_callback       after_tool_callback      on_tool_error_callback
on_event_callback          close
```

### The mechanic that matters

`before_tool_callback` returns `Optional[dict]`.

- Return `None` — the tool runs.
- Return a dict — the tool never runs, and your dict becomes the tool
  result handed back to the model.

That is a real interceptor, not a listener. The ADK docs recommend
plugins over per-agent callbacks for security work, because a plugin
registers on the Runner instead of on each agent.

### Signature gotcha

Plugin hooks are keyword-only, and the tool hooks use `tool_args`:

```python
async def before_tool_callback(
    self, *, tool: BaseTool, tool_args: dict[str, Any], tool_context: ToolContext
) -> Optional[dict[str, Any]]: ...

async def after_tool_callback(
    self, *, tool: BaseTool, tool_args: dict[str, Any],
    tool_context: ToolContext, result: dict[str, Any],
) -> Optional[dict[str, Any]]: ...
```

The agent-level callback version uses `args`, not `tool_args`. Same idea,
different parameter name. This costs people an hour.

Also note: plugins are not supported by the `adk web` interface. If your
workflow uses plugins you must run it without the web UI.

## What is inside ADK's Model Armor plugin

Shipped in 2.8.0. Import path:

```python
from google.adk.integrations.model_armor import ModelArmorConfig, ModelArmorPlugin
```

Two findings from reading `_plugin.py` and `_config.py`, both worth saying
out loud in an interview.

**1. It implements `before_model_callback` and `after_model_callback`.
Nothing else.** Google's own model-layer guardrail never sees a tool call.
So "the model layer cannot stop action harm" is not a theory — it is the
shape of the vendor's own code. If your agent has tools, you write the
tool gate yourself.

**2. `block_on_screening_failure` defaults to `True`.** Fail closed. That
is the right default and also a real availability trade-off: a Model Armor
outage becomes an agent outage. Set it deliberately, per workflow.

Config surface:

```python
ModelArmorConfig(
    prompt_template_name="projects/P/locations/europe-west4/templates/nda-in",
    response_template_name="projects/P/locations/europe-west4/templates/nda-out",
    input_blocked_message="I can't process that request.",
    output_blocked_message="I can't share that response.",
    block_on_screening_failure=True,
)
```

At least one of the two template names must be set, or the model validator
raises. Templates must share a location.

## What the demo proves

Five scenarios in `demo.py`. An NDA review agent, two roles, five tools
classified by blast radius, all rules in `policy.yaml`.

The interesting one is scenario 5. A retrieved NDA (`NDA-0442`) contains an
injected instruction telling the agent to send the document to
`data-exfil.example`. The scripted model obeys the document, which is what
a real model often does.

### Both layers on

```
5. Injected instruction inside a retrieved NDA
  AUDIT {"layer":"model","hook":"after_tool","effect":"redact","detector":"injection_pattern"}
  AUDIT {"layer":"orchestration","effect":"deny","rule":"R-014","blast_radius":"outbound"}
  BLOCKED request_signature: policy_denied [R-014] counterparty domain
          is not on the approved allowlist (got 'data-exfil.example')
```

### `--no-scan` (model layer off)

The agent reads the injected text in full and obeys it. Policy still
denies `R-014`. Nothing leaves.

### `--no-policy` (orchestration layer off)

This is the run to remember.

```
  AUDIT {"layer":"model","hook":"after_tool","effect":"redact"}
  tool ok read_clause: {'text': '[content withheld: embedded instruction detected]'}
  tool ok request_signature: {'status': 'sent', 'to': 'data-exfil.example'}
```

The scanner caught the injection and redacted it. The document was sent
anyway. **Content screening worked perfectly and the exfiltration still
happened.**

Bonus from that run: scenario 4, the cross-tenant read, was still blocked
with policy off — because the tool filters by tenant itself. That is the
data layer doing its own job.

## Why policy is testable and a content filter is not

`test_policy.py` has 10 assertions and runs in 0.03 seconds. It asserts
that no role holds a destructive tool, that every tool declares a blast
radius, and that an unapproved domain is denied outright rather than
queued for a human to rubber-stamp.

That last one is a real bug class: an approval gate becomes the weakest
link when it fires *before* the allowlist check. Order your rules so hard
denies win over approval prompts.

You cannot write that suite against a content filter. The best you get
there is an eval set and a pass rate.

## Testing guardrails without an API key

`ScriptedLlm` subclasses `BaseLlm` and replays a fixed script of turns:

```python
class ScriptedLlm(BaseLlm):
    model: str = "scripted-llm"
    script: list[dict[str, Any]] = []
    cursor: int = 0

    async def generate_content_async(self, llm_request, stream=False):
        turn = self.script[self.cursor]; self.cursor += 1
        ...yield LlmResponse(...)
```

The Runner, the plugin dispatch and the tool execution are all the real
thing. Only the token generator is faked. This is how guardrail tests
belong in CI: deterministic, free, offline.

## LangChain to ADK mapping

| What you want | LangChain | ADK |
|---|---|---|
| Screen the prompt | callback handler `on_llm_start`, or a chain step | `before_model_callback` |
| Screen the output | `on_llm_end` | `after_model_callback` |
| Gate a tool call | wrap the tool, or `on_tool_start` (cannot block cleanly) | `before_tool_callback` — return a dict and the tool never runs |
| Filter tool output | wrap the tool | `after_tool_callback` — return a dict to replace the result |
| Apply to every agent | attach handlers per chain/agent | `plugins=[...]` on the `Runner` |
| Interrupt for approval | LangGraph `interrupt_before` | return `{"error": "approval_required"}`, or `LongRunningFunctionTool` |
| Per-request identity | `RunnableConfig` `configurable` | `session.state`, read via `tool_context.state` |

The real difference: a LangChain callback handler is mostly an observer.
`on_tool_start` cannot cleanly cancel the call. So people enforce policy by
wrapping each tool, and the guardrail smears across every tool definition.
Twenty tools means twenty places to review.

In ADK, `before_tool_callback` is an interceptor with a documented
short-circuit, and a plugin registers once. One place to review, one place
to audit.

LangGraph is the fair comparison, not classic LangChain. A policy node
before the tool node gives the same interception, and `interrupt_before`
gives the approval gate. ADK just ships the hook instead of you building
the graph.

## Production wiring

```python
from google.adk.integrations.model_armor import ModelArmorConfig, ModelArmorPlugin
from ndaguard.plugins import PolicyPlugin

agent = LlmAgent(name="nda_reviewer", model="gemini-2.5-flash",
                 instruction="...", tools=ALL_TOOLS)

runner = InMemoryRunner(
    agent=agent, app_name="nda_guard",
    plugins=[
        ModelArmorPlugin(config=ModelArmorConfig(
            prompt_template_name="projects/P/locations/europe-west4/templates/nda-in",
            response_template_name="projects/P/locations/europe-west4/templates/nda-out",
            block_on_screening_failure=True,
        )),
        PolicyPlugin(),
    ],
)
```

Environment:

```
GOOGLE_GENAI_USE_VERTEXAI=1
GOOGLE_CLOUD_PROJECT=<project>
GOOGLE_CLOUD_LOCATION=europe-west4
MODEL_ARMOR_TEMPLATE_ID=<template>
```

Keep `PolicyPlugin` unchanged. That is the point — the policy layer does
not care which model or which vendor filter sits above it.
