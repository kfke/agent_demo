# Answers, with the runnable proof

Companion to [`03-agentic-architecture-questions.md`](03-agentic-architecture-questions.md).

Where a scenario id appears (`R4`), run it:

```bash
python reliability_demo.py R4
```

Questions 2, 6, 7, 8, 9 and 10 are narrative — no code proves an opinion.
They are marked **narrative** and the sections say what to say.

---

## Q1 — Timeouts, retries, circuit breakers, idempotency

**Proof: R1–R5.** Implemented in [`ndaguard/reliability.py`](../ndaguard/reliability.py).

### The one sentence

Retry safety is a property of the **tool**, not of the **error** — so it is
read off `blast_radius` in `policy.yaml`, the same field that decides who
may call the tool at all.

```
read        retry freely
write       retry only with an idempotency key
outbound    retry only with an idempotency key
destructive never retry automatically
```

There is a test pinning this across both files
(`test_every_policy_tool_has_a_retry_class`), so adding a tool with a new
blast radius fails CI rather than silently inheriting a default.

### Why "just add retries" is wrong

R2 is the whole argument in one run. An outbound send times out at 60ms.
The caller sees a failure. But:

```
>> caller saw: ok=False stopped_by=not_retryable
>> but the document WAS sent: sender.sent = ['anon-1']
```

A timeout tells you the **response** did not arrive. It tells you nothing
about whether the **request** landed. Retrying here sends the NDA for
signature twice. So the runtime refuses.

R3 makes the same call retryable by adding the missing state — an
idempotency key the downstream honours. Now the retry is safe: two calls
reach the service, one send is recorded, and a repeated *turn* replays from
the store without calling the tool at all.

The key must be **derived deterministically** from the turn —
`sig:NDA-0119:turn-7` — not generated fresh at retry time. A random uuid per
attempt is not an idempotency key, it is a unique id for each duplicate.

### Order of operations

```
idempotency replay  ->  circuit breaker  ->  budget check
    ->  attempt  ->  classify failure  ->  affordable backoff
```

Each position is deliberate. Replay first, or a retried turn re-sends.
Breaker before budget, because failing fast on a known-bad dependency
should cost 0ms (R5: `call 2: stopped_by=breaker, 0.0ms`).

### Cascading latency: budget, not timeout

A per-call timeout composes badly. Three tool rounds × three attempts × a
10s timeout is a 90s turn nobody planned. A **budget** is absolute for the
whole turn and every downstream call gets what is *left*:

```
b.slice_ms(want_ms) -> min(want_ms, remaining_ms)
```

R4 shows the consequence. A 100ms backoff is affordable in isolation and
unaffordable with 18ms left, so the call stops instead of sleeping into a
deadline it cannot meet:

```
TRACE event=backoff_would_exceed_budget backoff_ms=100 remaining_ms=18
>> stopped_by=budget after 1 attempt, 132ms elapsed
```

### Circuit breakers

Per dependency, never global — one sick tool must not trip the rest
(`test_breaker_is_per_dependency`). Half-open lets exactly one probe
through; a failed probe reopens immediately rather than re-running the
whole threshold.

### State management — the part they are actually asking about

Three lifetimes, three homes. Conflating them is the usual bug.

| State | Lifetime | Home | Example |
|---|---|---|---|
| Session | across turns, survives redeploy | durable store / checkpointer | role, tenant, granted approvals |
| Turn | one user turn | in-memory, request-scoped | trace id, budget, idempotency keys |
| Process | across sessions | per-pod, or Redis if shared | circuit breaker state, rate limiters |

In **ADK** the session store is the `SessionService` and turn state rides on
`tool_context.state`; the wrapper above goes in a `before_tool_callback`
plugin, exactly where `PolicyPlugin` already sits. In **LangGraph** session
state is the checkpointer, and the wrapper is a node in front of the tool
node. Breaker state belongs in neither — it is process state, and putting
it in session state is how you get a breaker that never opens.

---

## Q2 — ADK vs Strands vs LangChain/LangGraph · narrative

### The decision

| Pick | When | Costs you |
|---|---|---|
| **ADK** | GCP/Vertex shop; you want interception (`before_tool_callback`) as a first-class hook, plugins registered once on the Runner, built-in eval and Agent Engine deploy | Young API surface that moves between versions |
| **LangGraph** | Control flow *is* the hard part — cycles, branching, durable checkpointed state, `interrupt` for human approval, resume days later | You build the guardrails yourself; the graph gets big |
| **Strands** | Bedrock shop, model-driven loop, minimal ceremony to a working agent | Less control over the loop; smaller ecosystem |
| **Plain LangChain** | Prototyping and glue | Callbacks are *observers*, not interceptors — see below |

### The concrete pain (the half they actually care about)

Classic LangChain's `on_tool_start` cannot cleanly cancel a call. So
authorization gets enforced by wrapping each tool. Twenty tools means
twenty places to review and twenty places to forget. The failure is not
dramatic: someone adds tool twenty-one, ships it without the wrapper, and
there is no central test that can catch it because there is no central
place. You find out in an audit.

The fix is structural, not procedural — move to a runtime where the hook
returns a value that **replaces** the tool call. In ADK,
`before_tool_callback` returning a dict means the tool never runs and the
dict becomes its result. One place to review, one place to test. That is
why `PolicyPlugin` is 30 lines and covers every tool including ones written
after it.

A second one worth having ready: picking a framework with no durable
checkpointing for a workflow that needed two-day human approval. The pause
had to be bolted onto external state, and in-flight approvals were lost on
every redeploy. That is a LangGraph-shaped problem and choosing otherwise
cost a re-architecture.

**Say the version caveat out loud.** ADK moved `ModelArmorPlugin` into
`google.adk.integrations.model_armor`, and plugin hooks take `tool_args`
where agent-level callbacks take `args`. Verified on 2.8.0
([`02-adk-implementation.md`](02-adk-implementation.md)) — that kind of
churn is a real operational cost of an early framework, and naming it is
better than pretending it is stable.

---

## Q3 — Malformed tool calls and schema drift

**Proof: R6.** Implemented in [`ndaguard/contracts.py`](../ndaguard/contracts.py).

Four layers, and the ordering is the answer:

**1. Validate before execute.** The model is an untrusted producer of tool
calls. `ToolSchema.validate` returns a list of problems, not a boolean, so
the repair message can be specific. Note it rejects `True` for an `int`
field — `bool` is an `int` subclass in Python and an agent passing `True`
for a count is a real bug that `isinstance` alone waves through.

**2. Bounded repair.** Hand the problems back and let the model try again,
at most `max_repairs` times:

```
Your call to redline_clause was rejected before it ran:
- argument 'clause_count' must be int, got str
Emit the call again with those fixed, or say you cannot.
```

The bound is the guardrail. An unbounded repair loop against a model that
cannot satisfy the schema will burn the entire token budget failing, which
is a genuine outage mode and not a theoretical one.

**3. Faults are data, not exceptions.** In `call_tool`, `except Exception`
converts a tool fault into a `CallResult`. One bad turn degrades one turn;
nothing propagates into the process.

**4. Drift is caught at boot, not by a user.** The build pins the schema
versions it was tested against; `drift_against` compares them to the live
registry at readiness:

```
DRIFT tool 'redline_clause' pinned at v1, runtime has v2
DRIFT tool 'purge_document' pinned at v1 has disappeared
DRIFT tool 'request_signature' appeared and was never pinned
```

That third line matters as much as the first two — an *unpinned new tool*
is a tool nobody wrote policy for, and `policy.py` defaults it to deny
(`R-000`). Fail the readiness probe and the rollout stops before traffic
arrives.

---

## Q4 — Tracing one interaction across GKE

**Proof: R8.** `Trace` / `Telemetry` in [`ndaguard/reliability.py`](../ndaguard/reliability.py).

**One id per user turn, stamped by every layer.** Model layer, policy layer
and tool all write the same `trace_id`. R8 ends with a policy denial and a
tool retry under one id:

```
tr-4c052cb7/01-search_ndas   event=ok attempt=1
tr-4c052cb7/02-read_clause   event=attempt_failed attempt=1
tr-4c052cb7/02-read_clause   event=ok attempt=2
tr-4c052cb7/03-policy_gate   layer=orchestration event=deny rule=R-014
```

A user says "it refused around 14:32". That is one filter, not three log
searches and a guess. This is backlog item 4 in `CLAUDE.md`, named as a
pain point in `docs/01` — two layers that can both say no means two places
to look unless they share an id.

### What to emit

Per **turn**: trace id, session id, user id (or a pseudonym), tenant, agent
version, prompt version, policy version, model id, total tokens, cost,
outcome.

Per **model call**: span with model, input/output tokens, latency, finish
reason, whether a filter fired.

Per **tool call**: span with tool name, blast radius, policy rule id,
attempt number, breaker state, remaining budget. `rule_id` on the span is
what turns "it said no" into "R-014 said no".

### GKE mechanics

- OTel Collector as a DaemonSet; app exports OTLP to the node-local agent.
- Propagate **W3C `traceparent`** on every outbound HTTP call a tool makes,
  so downstream services join the same trace instead of starting their own.
- Structured JSON logs carrying `trace_id` — Cloud Logging correlates logs
  to spans automatically when the field is present.
- **Exemplars** on latency histograms: click the p99 bucket in the metric,
  land on a real trace. This is the single highest-value thing to wire.
- Sample head-based at a low rate but **tail-sample every error and every
  policy denial at 100%**. The interesting traces are rare by construction.

---

## Q5 — Throttling, degradation, SLOs

**Proof: R7.** Implemented in [`ndaguard/degradation.py`](../ndaguard/degradation.py).

### Three SLOs, not one

```
availability          0.995   turn completed without a 5xx
latency_p95_under_8s  0.95    a turn is many calls; p95 is the honest one
task_success          0.90    judged offline on sampled traffic
```

The third is the one people leave out, and it covers the failure that
actually annoys users: the agent answered, on time, with something useless.
Availability alone cannot see it.

### The ladder

Under throttling you have three honest options and one dishonest one. The
dishonest one is queueing everything and letting p99 go to 40 seconds.

```
 throttle   budget  tier      model               cost     p95  serves
       0%     100%  primary   gemini-2.5-pro      1.00  4200ms  full tool-using answer
      15%      90%  cheap     gemini-2.5-flash    0.08  1100ms  full answer, weaker reasoning
      50%      70%  cached    none                0.00    30ms  last known good for this query shape
      50%      70%  refuse    none                0.00     5ms  honest "try again in a minute"
      80%       0%  refuse    none                0.00     5ms  honest "try again in a minute"
```

Two rules make it defensible. **Exhausted error budget refuses regardless
of throttle rate** — you stop making promises you cannot keep
(`test_exhausted_error_budget_refuses_regardless_of_throttle`). And you
**shed at the edge**, before the expensive call, not by timing out after
you have already paid for it.

### Backpressure

Bounded concurrency *and* a bounded queue. An unbounded queue converts a
throughput problem into a latency problem and then into an OOM. R7: 10
arrivals, 6 admitted, 4 rejected at the door. Rejecting fast is kinder than
accepting work the client will abandon.

### When the SLO is about to break

Alert on **burn rate**, multi-window multi-burn-rate: a fast burn (2% of
budget in an hour) pages; a slow burn (10% in three days) opens a ticket.
A single 429 is not an incident. And when the budget is spent, the freeze
is automatic — no new prompt or model version promotes until it recovers,
which ties this straight into Q6.

---

## Q6 — Versioning and promotion · narrative

### Three artifacts, versioned separately

Prompt, policy, and model id are **independently deployable config**, not
code constants. `policy.yaml` already carries `version: 4` and every audit
row stamps it — so a denial from last Tuesday can be replayed against the
policy that was actually live.

The reason to separate them: a prompt rollback should be a config change
measured in seconds, not a container rebuild.

### Promotion gate

```
sandbox (synthetic data, loose policy)  --gate-->  production
```

The gate is: policy file present and reviewed, eval suite passes at
threshold, red-team pass, error budget not exhausted. Speed lives *before*
the gate — iteration in the sandbox is minutes. Rigour lives *at* it.

### Canarying without breaking sessions

The hard part is session affinity. **Pin the variant at session creation
and never re-evaluate mid-session.** A user whose turn 3 runs on a
different prompt than turns 1–2 gets incoherence, and your eval numbers get
contaminated by mixed-variant sessions.

Ramp 1% → 5% → 25% → 100%, gated on the same three SLOs plus cost per turn.
Compare on task success and p95, not on eyeballing outputs.

For a **model** swap specifically, run shadow mode first: same traffic to
both, only the incumbent's answer is served, and diff them offline. It
costs double for a week and catches regressions no offline eval predicted.

---

## Q7 — Operability for SREs · narrative

The goal: an SRE who has never read a tool implementation can still resolve
the page.

**Every alert names an action.** Not "agent error rate high" but
"`request_signature` breaker open for 5m → check the signature service,
runbook §3, expected user impact: signature sends fail, reads unaffected".
Blast radius is already in `policy.yaml`, so the alert can state impact
without the responder knowing the tool.

**Health vs readiness are different questions.** Liveness: process alive.
Readiness: model reachable, policy file parses, **schema drift check
passes** (R6), all breakers closed. A pod with an open breaker on a
critical tool should leave the rotation, not serve degraded answers
silently.

**Self-healing that already exists in this repo:** the circuit breaker
(recovers without a human), the degradation ladder (sheds instead of
falling over), bounded repair (stops burning tokens), and default-deny on
unregistered tools (a mystery tool fails closed).

**Reduce toil by making the audit log answer the common question.** "Why
did it refuse for this customer?" is the top ticket, and `rule=R-014` in a
trace answers it without escalation to the team that wrote the agent.

**Kill switches as config:** disable a single tool, force a ladder tier,
freeze promotion. All three should be flags an SRE can flip without a
deploy.

---

## Q8 — The subtly wrong result · narrative, bring your own

Do not invent an incident. Pick a real one and tell it in this shape — and
if you genuinely have not hit one, say what you would watch for instead.
That answers honestly and still shows the reasoning.

**The failure classes that actually happen:**

- A tool returns an empty list and the agent reports "no results found"
  when the truth was "the query errored". Empty and failed look identical
  to a model. Unit tests pass because the tool returned `200 []`.
- Retrieval pulls a stale or wrong-tenant chunk; the answer is fluent,
  sourced, and about the wrong contract.
- Silent truncation — a long document is cut at the context limit and the
  summary omits the clause that mattered. Nothing errors.
- A schema change makes an optional field always absent; the agent quietly
  stops using it.

**How it gets caught in production, in rough order of value:**

1. **Sampled LLM-judge on real traffic** scoring groundedness and task
   success — this is what makes `task_success` an SLO rather than a wish.
2. **Canary questions** with known-correct answers, run continuously
   against production. A drop is unambiguous.
3. **Distribution monitoring** — tool-call mix, empty-result rate, answer
   length, refusal rate. The empty-result-rate jump is what would have
   caught failure class one.
4. **Business metric guardrails** — downstream correction rate, human
   override rate. Slow but honest.
5. **User feedback**, which is where it usually actually comes from, and
   which arrives weeks late.

**What changes afterwards:** the tool stops conflating empty with failed
(distinct return shapes, and the empty case gets its own metric); a
regression case enters the eval suite; and the alert moves from
"error rate" to "output distribution".

---

## Q9 — Human-in-the-loop · narrative + partial proof

`policy.yaml` already implements the routing decision:

```yaml
request_signature:
  blast_radius: outbound
  requires_approval: true
```

**Blast radius decides who needs a human.** Reads never do. Writes usually
do not. Outbound and destructive do. That is what keeps the latency tax off
the 95% of calls that are reads — the answer to "without adding
unacceptable latency" is *not* faster approvals, it is far fewer of them.

**Asynchronous, not blocking.** The approval must not hold a request
thread. The turn suspends to durable state and resumes on an event —
LangGraph `interrupt`, or ADK's `LongRunningFunctionTool`. That is backlog
item 1 in `CLAUDE.md`, and today's implementation is honestly incomplete:
`PolicyPlugin` returns `{"error": "approval_required"}` and the turn ends.
It routes correctly but does not durably pause. Say that plainly if asked —
it is a design gap, not a hidden one.

**Not a single point of failure:**

- Approver *groups*, never a named individual.
- A timeout with an explicit default, and the default is **deny** for
  anything outbound or destructive.
- Escalation after N minutes to a second group.
- The queue is durable, so a redeploy does not drop pending approvals.

**The trap to avoid**, already tested in this repo: an approval gate placed
*before* the argument checks turns humans into rubber stamps for calls that
should have been denied outright. `test_domain_allowlist_beats_approval`
pins the ordering — an unapproved domain is denied, never queued.

---

## Q10 — Evaluating models beyond accuracy · narrative

### The dimensions that decide a production choice

| Dimension | Metric | Why it decides things |
|---|---|---|
| Quality | task success by judge, on *your* traffic | Public benchmarks do not contain your documents |
| Latency | p50 **and p95/p99**, per turn not per call | A turn is many calls; p95 per call becomes p99+ per turn |
| Cost | per *turn*, including retries and repairs | A cheap model that needs two repair rounds is not cheap |
| Throughput | tokens/sec and your actual rate limit | Often the real constraint, not price |
| Stability | variance across identical runs; **tool-call validity rate**; format adherence | The one that gets skipped and then hurts |

**Stability deserves the emphasis.** For an agent, "how often does it emit
a well-formed tool call" matters more than raw reasoning quality, because
every malformed call costs a repair round (R6) — latency, tokens, and a
chance of falling through to the fallback. A model that is 3% smarter and
8% worse at schema adherence is a net loss in a tool-using agent.

### Shape of the trade-off to describe

The honest one is the ladder in R7: the cheap tier is ~12× cheaper and ~4×
faster with weaker reasoning. On the NDA workload that split cleanly by
task — retrieval and summarisation went to the cheap model with no
measurable task-success drop, clause interpretation stayed on the strong
one. Routing by task type beat picking one model for everything.

The general principle: **evaluate per step, not per agent.** An agent is
five different jobs wearing one name, and they do not all need the same
model.

*(Numbers above are the ladder's configured figures, not a measurement —
substitute your own before quoting them.)*
