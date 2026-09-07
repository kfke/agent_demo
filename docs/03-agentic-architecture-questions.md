# Category 1 — Agentic Architecture: the question bank

Ten questions, verbatim, with what each one is really testing and where in
this repo the answer is demonstrable rather than merely assertable.

Column **Proof** points at code you can run in front of someone.

| # | Topic | Focus | Proof |
|---|---|---|---|
| 1 | Timeouts, retries, circuit breakers, idempotency | Reliability patterns, state handling | `reliability_demo.py` R1–R5 |
| 2 | ADK vs Strands vs LangChain/LangGraph | Framework trade-offs | `docs/02` §LangChain mapping, narrative |
| 3 | Malformed tool calls, schema drift | Error isolation, validation, fallback | `reliability_demo.py` R6 |
| 4 | Tracing one interaction across GKE services | Observability, OTel, correlation | `reliability_demo.py` R8 |
| 5 | Provider throttling, graceful degradation, SLOs | Capacity, backpressure, error budget | `reliability_demo.py` R7 |
| 6 | Versioning and canarying prompts/models | CI/CD for non-deterministic software | `policy.yaml` `version:`, narrative |
| 7 | Operability for SREs who don't know the internals | Runbooks, health checks, self-healing | `docs/04` §Operability |
| 8 | A subtly wrong result unit tests missed | Production validation | narrative — supply your own incident |
| 9 | Human-in-the-loop without latency or SPOF | Escalation, async approvals | `policy.yaml` `requires_approval`, backlog 1 |
| 10 | Evaluating LLMs beyond accuracy | Operational model selection | `docs/04` §Model selection |

---

## The questions

### 1. Reliability across multiple LLM calls and tool invocations

> "When designing an agent that must make multiple LLM calls and tool
> invocations, how do you enforce timeouts, retries, and circuit breakers
> without creating cascading latency or duplicate side effects? Walk me
> through your state management."

**Focus:** Reliability patterns, idempotency, state handling in LangGraph/ADK.

**What is really being tested:** whether you know that *retry* and *side
effect* are in tension, and that a timeout per call is not a budget.

### 2. Framework choice

> "You mentioned ADK, Strands, and LangChain/LangGraph. In what scenario
> would you choose one over the others for a production agent? Give me a
> concrete example where the wrong choice caused operational pain."

**Focus:** Framework trade-offs, real-world consequences.

**What is really being tested:** whether you have opinions from operating
these, or only from reading their landing pages. The second half of the
question is the real one.

### 3. Non-deterministic failures

> "How do you handle non-deterministic failures in agent workflows—for
> instance, an LLM returns a malformed tool call, or a tool schema changes
> unexpectedly? What guardrails do you put in place to prevent a single bad
> output from taking down the entire pipeline?"

**Focus:** Error isolation, schema validation, fallback strategies.

**What is really being tested:** blast-radius containment. One bad turn
should degrade one turn.

### 4. Tracing across services

> "Describe your approach to tracing a single agent interaction across
> multiple services and tools in GKE. What telemetry do you emit, and how do
> you correlate logs, traces, and metrics to debug a user-reported issue?"

**Focus:** Observability, OpenTelemetry, correlation.

**What is really being tested:** can a user hand you a timestamp and a
complaint, and can you get to the exact span.

### 5. Throttling and graceful degradation

> "Imagine your agent is under heavy load and the LLM provider starts
> throttling. How do you design for graceful degradation? What SLOs would
> you set for the agent, and what happens when they are about to be
> breached?"

**Focus:** Capacity planning, SLO-based alerts, backpressure.

**What is really being tested:** whether you shed load deliberately or let
the queue do it for you.

### 6. Versioning and promotion

> "What's your strategy for versioning and promoting an agent from
> development to production? How do you canary a new model or a new prompt
> without affecting ongoing user sessions?"

**Focus:** CI/CD for non-deterministic software, feature flags.

**What is really being tested:** that a prompt is a deployable artifact,
and that session affinity is the hard part of canarying an agent.

### 7. Operability

> "How do you ensure that your agent can be safely operated by a team of
> SREs who may not understand the internals of every tool call? What
> operational runbooks, health checks, or self-healing mechanisms do you
> build into the agent runtime?"

**Focus:** Operability, reducing toil, runbook automation.

**What is really being tested:** whether your on-call page has an action
attached to it.

### 8. The subtly wrong result

> "Tell me about a time when an agent you built produced a subtly incorrect
> result that wasn't caught by unit tests. How did you detect it in
> production, and what changes did you make to monitoring or validation?"

**Focus:** Production incident, validation beyond testing.

**What is really being tested:** intellectual honesty, and whether your
monitoring watches outputs or only watches HTTP 200s.

### 9. Human-in-the-loop

> "What role do you see for human-in-the-loop in enterprise agent workflows?
> How do you implement that without adding unacceptable latency or creating
> a single point of failure?"

**Focus:** Escalation design, asynchronous approvals.

**What is really being tested:** whether your approval gate is synchronous
(and therefore a SPOF and a latency tax) or durable and asynchronous.

### 10. Model evaluation

> "How do you evaluate the performance of different LLMs for a specific
> agent task beyond simple accuracy? What metrics matter for production—
> latency, cost, throughput, stability? Give an example of a trade-off you
> had to make."

**Focus:** Model selection from an operational perspective.

**What is really being tested:** whether you have ever measured p95 rather
than quoting a benchmark.

---

Answers, with the runnable proof for each, are in
[`04-reliability-patterns.md`](04-reliability-patterns.md).
