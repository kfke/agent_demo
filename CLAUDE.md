# CLAUDE.md

Context for Claude Code working in this repo.

## What this is

A teaching and reference project about **where agent guardrails belong**:
at the model layer, at the orchestration layer, or both. It answers that
with a runnable Google ADK agent that enforces guardrails at both layers
and can be run with either layer switched off to show what each one
actually catches.

Domain: NDA / contract review, standing in for a document-management
compliance workflow. All data is synthetic.

Two audiences: a person preparing to discuss this in an interview, and a
person who wants a working reference implementation to copy.

A second track was added later, answering an **Agentic Architecture**
question set (reliability, observability, degradation). Same house style:
deterministic primitives, no ADK imports, asserted in CI.

## Read these first

- `docs/01-guardrail-layers.md` — the concepts, trade-offs, and a spoken
  answer. Start here.
- `docs/02-adk-implementation.md` — verified ADK hook map, what is inside
  ADK's Model Armor plugin, demo results, LangChain mapping.
- `docs/03-agentic-architecture-questions.md` — the 10-question bank.
- `docs/04-reliability-patterns.md` — the answers, each pointing at a
  runnable scenario.
- `docs/05-interview-answers.md` — the same answers as spoken delivery.
  Thesis, script, the detail to drop, the follow-up. Q8 is deliberately
  left as a template: do not fill it with invented experience.
- `README.md` — quickstart.

## Layout

```
policy.yaml              the governance surface; every rule lives here
ndaguard/policy.py       deterministic engine, ZERO adk imports on purpose
ndaguard/plugins.py      PolicyPlugin (before_tool) + ContentScanPlugin (before_model/after_tool)
ndaguard/tools.py        tools; retrieval filtered by caller's tenant
ndaguard/scripted_llm.py BaseLlm subclass replaying a fixed script
demo.py                  five guardrail scenarios, three run modes
test_policy.py           10 assertions over policy.yaml

ndaguard/reliability.py  Trace, Budget, CircuitBreaker, IdempotencyStore, call_tool
ndaguard/contracts.py    ToolSchema, SchemaRegistry, RepairLoop
ndaguard/degradation.py  SLO, ErrorBudget, DegradationLadder, LoadShedder
reliability_demo.py      eight scenarios R1..R8
test_reliability.py      23 assertions over the reliability primitives
```

## Commands

```bash
pip install -r requirements.txt

python demo.py                # both layers on
python demo.py --no-scan      # model layer off  -> policy still holds
python demo.py --no-policy    # policy off       -> scanner cannot save you
python demo.py --openai       # live OpenAI through ADK LiteLLM

python reliability_demo.py    # all eight; or pass R1..R8 to pick
python -m pytest -q           # 33 assertions, ~0.1s
```

No API key needed for the default scripted run. Live OpenAI runs use ADK's
`google.adk.models.lite_llm.LiteLlm` wrapper and require `OPENAI_API_KEY`.
Verified on `google-adk==2.8.0`, Python 3.13.3.
`reliability_demo.py` imports no ADK at all.

## Conventions to preserve

- **`ndaguard/policy.py` must not import from `google.adk`.** Policy has to
  be testable without booting an agent and portable if the framework
  changes. If a change would add that import, restructure instead.
- **Every rule goes in `policy.yaml`, never in a prompt and never
  hardcoded in Python.** That is the whole "policy as code" claim.
- **Every `Decision` names the rule that made it.** `rule_id` is the audit
  trail. New rules get an `R-0xx` id.
- **Hard denies must win over approval prompts.** An unapproved value gets
  denied outright, never queued for a human to rubber-stamp. There is a
  test for this (`test_domain_allowlist_beats_approval`) — keep it.
- **Every tool declares a `blast_radius`** of `read`, `write`, `outbound`
  or `destructive`. A test enforces this.
- **Default deny.** An unregistered tool is denied (`R-000`), not allowed.
- Data stays synthetic. Do not add real contract text, names or PII.
- `--no-policy` deliberately runs an unsafe agent. Demo only.
- **`reliability.py`, `contracts.py` and `degradation.py` must also stay
  free of `google.adk` imports.** Same reason as `policy.py`. The ADK
  binding belongs in `plugins.py` and nowhere else.
- **Retry safety is read off `blast_radius`, never hardcoded.**
  `test_every_policy_tool_has_a_retry_class` pins `policy.yaml` and
  `reliability.py` together — a new blast radius fails CI rather than
  silently inheriting a default.
- **Never retry a `write`/`outbound` call without an idempotency key**, and
  derive the key from the turn, not from a fresh uuid per attempt. R2/R3
  in `reliability_demo.py` exist to make that concrete.

## Backlog

Ordered roughly by value.

**Note on 2 and 4:** the primitives now exist in `ndaguard/reliability.py`
and are demonstrated standalone in `reliability_demo.py`, but they are
**not wired into the ADK path**. `plugins.py` still has no trace id and
`demo.py` still has no budget. Wiring them is the remaining work, and it is
the highest-value item on this list because it makes both demos one system.

1. **Real human-in-the-loop.** Today `needs_approval` returns a dict and
   the turn ends. Replace with `LongRunningFunctionTool` so the run
   genuinely pauses and resumes on approval. Called out honestly as a gap
   in `docs/04` Q9.
2. **Wire `Budget` and `call_tool` into `PolicyPlugin`.** Per-tool
   timeouts, `ADK_MAX_LLM_CALLS`, a per-session tool-call cap, and a
   scenario where a loop is cut off.
3. **OPA instead of the Python engine.** Rewrite `policy.yaml` as Rego and
   call OPA from `PolicyPlugin`. Keep the same tests passing — that is the
   proof the policy layer is portable.
4. **Thread `Trace` through `plugins.py`** so guardrail audit rows and
   reliability spans share one id. Currently only `reliability_demo.py` R8
   shows the correlated view.
5. **Signed audit sink.** Append-only, tamper-evident. Currently the audit
   log is a Python list printed to stdout.
6. **Model Armor false-positive harness.** Feed real legal clause language
   through the screening templates and measure the block rate. The
   over-blocking trade-off in `docs/01` deserves numbers.
7. **LangGraph parallel implementation.** Same `policy.yaml`, a policy node
   before the tool node. Makes the framework comparison concrete.

## Notes for future edits

ADK APIs have moved between versions. Before trusting anything in
`docs/02`, re-verify against the installed package:

```bash
python -c "import google.adk, importlib.metadata as m; print(m.version('google-adk'))"
python -c "from google.adk.plugins.base_plugin import BasePlugin; print([m for m in dir(BasePlugin) if not m.startswith('_')])"
```

The Model Armor plugin lives at `google.adk.integrations.model_armor`, not
under `google.adk.plugins`. If that import breaks, grep the installed
package for `model_armor` rather than guessing.
