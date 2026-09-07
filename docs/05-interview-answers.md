# The answers, as you would say them

[`04-reliability-patterns.md`](04-reliability-patterns.md) is the reference.
This is the delivery.

Each question gets four things:

- **Thesis** — one sentence. If you only get to say one thing, say this.
- **The answer** — spoken, 60–90 seconds. Read it aloud once; if it does
  not sound like you, change the words, keep the structure.
- **Detail to drop** — the specific fact that separates "I read about this"
  from "I have done this."
- **If they push** — the follow-up that is actually coming.

## How to use it

Lead with the thesis. Stop talking after 90 seconds. An interviewer who
wants more will ask, and the ask is worth more than the monologue.

The details are all from code in this repo that you have run. That is the
point of having built it — you are describing a thing that exists, not a
position you hold.

**Where a section says BRING YOUR OWN, do that.** An invented anecdote
survives about two follow-ups.

---

## Cheat sheet — the ten theses

1. Retry safety is a property of the **tool**, not of the **error**.
2. The axis that matters is whether the hook can **cancel** the call or only **watch** it.
3. The model is an **untrusted producer** of tool calls: validate before execute, bound the repair.
4. One trace id per turn, stamped by every layer — and tail-sample every error at 100%.
5. Three SLOs, not one. Shed at the **edge**. Page on **burn rate**.
6. Prompt, policy and model are versioned config; pin the variant at **session creation**.
7. Every alert names an **action** and an **impact** — blast radius gives you the impact for free.
8. Unit tests assert the tool returned 200. They cannot assert the answer was right.
9. Keep human-in-the-loop cheap by **needing it rarely**; approval must be durable and async.
10. Evaluate **per step, not per agent**. For tool use, schema adherence beats raw reasoning.

---

## Q1 — Timeouts, retries, circuit breakers, idempotency, state

**Thesis:** Retry safety is a property of the tool, not of the error.

### The answer

"I classify every tool by blast radius — read, write, outbound,
destructive. That one field drives three separate things: who may call it,
how much approval ceremony it needs, and whether a failed call may be
retried. Reads retry freely. Writes and outbound calls retry only if
there's an idempotency key. Destructive calls never retry automatically.

The reason is that a timeout tells you the response didn't come back. It
tells you nothing about whether the request landed. I have a demo where an
outbound signature request times out at 60 milliseconds and the caller sees
a clean failure — but the document was already sent. Retry that and the
counterparty gets two signature requests. So the runtime refuses to retry
unless there's a deduplication key, and the key is derived from the turn,
not generated per attempt. A random uuid at retry time isn't an idempotency
key, it's a unique id for each duplicate.

For latency I don't use per-call timeouts, I use a turn budget. Three tool
rounds times three retries times a ten-second timeout is a ninety-second
turn nobody planned. A budget is absolute for the whole turn and every
downstream call gets what's left. So a 100-millisecond backoff gets refused
when there are 18 milliseconds remaining — affordable in isolation,
unaffordable in context.

Circuit breakers are per dependency, never global. One sick tool shouldn't
trip the rest.

On state, there are three lifetimes and conflating them is the usual bug.
Session state — role, tenant, granted approvals — is durable and survives
redeploy. Turn state — trace id, budget, idempotency keys — is
request-scoped and in memory. Process state — breaker state, rate limiters
— is per-pod or in Redis. Putting breaker state in session state is how you
get a breaker that never opens. In ADK that's the SessionService,
`tool_context.state`, and the plugin instance respectively."

### Detail to drop

The order of operations, and why each position is chosen:

> "Replay, breaker, budget, attempt, classify, backoff. Replay first or a
> retried turn re-sends. Breaker before budget, because failing fast on a
> known-bad dependency should cost zero milliseconds — and in my demo it
> does, the second caller gets refused in 0.0ms."

### If they push

**"What if the downstream doesn't support idempotency keys?"**

"Then it isn't safely retryable and I say so rather than pretending. Two
options. Make it at-most-once and surface the ambiguity — to the user, or
to a human queue that can check. Or put a dedupe layer in front that I do
control: a table with a unique constraint on the key, written before the
call, so a replay can see that the attempt was already made. What I don't
do is retry and hope."

**"Where does the retry live — in the tool or the framework?"**

"The framework, once. If it lives in the tool it gets reimplemented twenty
times with twenty different backoff constants, and the twenty-first tool
ships without it."

---

## Q2 — ADK vs Strands vs LangChain/LangGraph

**Thesis:** The axis that matters operationally is whether the hook can
cancel the call or only watch it.

### The answer

"The axis I actually choose on is observer versus interceptor.

In classic LangChain, `on_tool_start` is effectively an observer — it can't
cleanly cancel the call. So people enforce authorization by wrapping each
tool, and the guardrail smears across every tool definition. Twenty tools
is twenty places to review and twenty places to forget. The failure isn't
dramatic: someone adds tool twenty-one, ships it without the wrapper, and
no test catches it because there's no central place to test. You find out
in an audit.

ADK's `before_tool_callback` returns an optional dict. Return None and the
tool runs; return a dict and the tool never runs and your dict becomes the
tool result. That's an interceptor with a documented short-circuit, and a
plugin registers once on the Runner, so it covers every agent, every
sub-agent, and every tool written after it. My policy plugin is
thirty-six lines and covers all of them.

So: ADK when I'm on GCP and want that interception plus the built-in eval
and Agent Engine deploy. LangGraph when control flow is the hard part —
cycles, branching, durable checkpointed state, `interrupt` for approvals
that resume days later. Strands for a Bedrock shop wanting a model-driven
loop with minimal ceremony. Plain LangChain for prototyping and glue."

### Detail to drop

The version churn, volunteered rather than extracted:

> "The honest cost of ADK is that it moves. Model Armor lives under
> `google.adk.integrations.model_armor`, not under `google.adk.plugins`
> where you'd look. And plugin hooks take `tool_args` where agent-level
> callbacks take `args` — same thing, different name. I verified that
> against 2.8.0 in a container rather than trusting a blog post."

Naming a framework's weakness while recommending it is what makes the
recommendation credible.

### If they push

**"Give me the concrete operational pain."** — This is the real question.
Pick one:

*The authz smear.* The LangChain wrapper story above. The tell is that the
hole was invisible: no error, no alert, no failing test. Just a tool that
was never gated, found in review.

*The missing checkpointer.* Choosing a framework with no durable
checkpointing for a workflow that needed two-day human approval. The pause
had to be bolted onto external state, and in-flight approvals were lost on
every redeploy. That's a LangGraph-shaped problem, and choosing otherwise
cost a re-architecture.

**BRING YOUR OWN if you have one.** If you don't, say "the sharpest one
I've hit is X" about something you genuinely hit, even if smaller.

---

## Q3 — Malformed tool calls and schema drift

**Thesis:** The model is an untrusted producer of tool calls.

### The answer

"Four things, and the ordering is most of the answer.

First, validate before execute. My validator returns a list of problems,
not a boolean, so the message back to the model can be specific —
'argument clause_count must be int, got str' rather than 'invalid input'.
Specific errors get repaired; vague ones get guessed at.

Second, bounded repair. Hand the problems back, let the model try again, at
most N times. The bound is the guardrail. An unbounded repair loop against
a model that can't satisfy the schema will burn the entire token budget
failing, and that's a real outage mode, not a theoretical one. After the
bound, you fall back to a degraded answer rather than stalling.

Third, tool faults are data, not exceptions. My call wrapper catches
broadly and converts a fault into a result object. One bad turn degrades
one turn. Nothing propagates into the process.

Fourth, drift is caught at boot, not by a user. The build pins the schema
versions it was tested against and compares them to the live registry in
the readiness probe. It reports three things: a version bump, a tool that
disappeared, and — the one people forget — a tool that appeared and was
never pinned. That third one matters most, because an unpinned new tool is
a tool nobody wrote policy for, and my policy engine default-denies it.
Fail readiness and the rollout stops before traffic arrives."

### Detail to drop

The `bool` trap. It is small, specific, and unmistakably from real code:

> "One detail — `bool` is a subclass of `int` in Python. An agent passing
> `True` for a clause count sails straight through a naive `isinstance`
> check. I reject it explicitly, because that's a real bug that looks like
> a valid call."

### If they push

**"What about a backward-compatible schema change?"**

"Additive optional fields are informational, not fatal. Required-argument
changes and type changes are fatal. The distinction has to be explicit,
otherwise every deploy either cries wolf or misses the real one."

**"Doesn't validation add latency?"**

"It removes it. A validated rejection costs microseconds; an unvalidated
bad call costs a full round trip to the tool, an error, and then a repair
round anyway."

---

## Q4 — Tracing across services in GKE

**Thesis:** One trace id per turn, stamped by every layer.

### The answer

"One trace id per user turn, and every layer stamps it — the content
filter, the policy gate, the tool call, each retry attempt. In my demo a
single turn ends up with a tool retry and a policy denial under one id, so
when a user says 'it refused at about 14:32', that's one filter, not three
log searches and a guess.

What I emit. Per turn: trace id, session, tenant, agent version, prompt
version, policy version, model, tokens, cost, outcome. Per model call: a
span with model, token counts, latency, finish reason, and whether a filter
fired. Per tool call: a span with tool name, blast radius, the policy rule
id, attempt number, breaker state, and remaining budget.

The rule id on the span is the one I'd emphasise. It turns 'it said no'
into 'R-014 said no', which is the difference between escalating to the
team that built the agent and answering the ticket.

On GKE: OTel Collector as a DaemonSet, app exports OTLP to the node-local
agent. The piece people miss is propagating W3C `traceparent` on every
outbound HTTP call a tool makes, so downstream services join the same trace
instead of starting their own. Structured JSON logs carrying `trace_id` —
Cloud Logging correlates logs to spans automatically when that field's
present.

On sampling: head-based at a low rate, but tail-sample every error and
every policy denial at 100%. The interesting traces are rare by
construction, so uniform sampling throws away exactly what you need."

### Detail to drop

> "The single highest-value thing to wire is exemplars on the latency
> histograms. You click the p99 bucket in the metric and land on a real
> trace. Without it, going from 'p99 is bad' to 'here's why' is a grep."

### If they push

**"Two layers can both block. How do you know which one did?"**

"That's exactly why they share an id — and I'd flag it as a genuine cost of
defence in depth. Two places that can say no is two places to look unless
they're correlated. My guardrail demo has that gap today: with policy
disabled a cross-tenant read still gets blocked by the tool's own filter,
but it reports no rule id and writes no audit row. Blocked correctly,
unexplainable. Same security outcome, much worse incident."

---

## Q5 — Throttling, degradation, SLOs

**Thesis:** Three SLOs, not one. Shed at the edge. Page on burn rate.

### The answer

"Three SLOs. Availability — the turn completed without a 5xx. Latency p95
under eight seconds, p95 rather than p99 because a turn is many calls and
the tails compound. And task success, judged offline by a model judge on
sampled traffic.

That third one is what people leave out, and it's the failure that actually
annoys users: the agent answered, on time, with something useless.
Availability can't see that at all.

When the provider throttles, I run a ladder. Under 10% throttle, primary
model. Ten to forty, drop to the cheap model — roughly twelve times cheaper
and four times faster, with weaker reasoning. Above forty, serve from cache
if there's a hit. Otherwise refuse quickly and honestly.

Two rules make that defensible. An exhausted error budget refuses
regardless of throttle rate — you stop making promises you can't keep. And
you shed at the edge, before the expensive call, not by timing out after
you've already paid for it.

Backpressure is bounded concurrency and a bounded queue. An unbounded queue
converts a throughput problem into a latency problem and then into an OOM.
Rejecting at the door is kinder than accepting work the client will
abandon.

Alerting is multi-window multi-burn-rate. Two percent of the budget in an
hour pages someone. Ten percent over three days opens a ticket. A single
429 is not an incident. And when the budget is spent, promotion freezes
automatically — no new prompt or model version ships until it recovers."

### Detail to drop

The honest option you are rejecting:

> "Under throttling you have three honest options and one dishonest one.
> Cheaper answer, cached answer, or a fast refusal. The dishonest one is
> queueing everything and letting p99 go to forty seconds — which is what
> you get by default if you don't choose."

### If they push

**"What's the SLO number and how did you pick it?"**

"I'd rather derive it than quote it. Task success at 90% means one in ten
turns needs a human correction — you set that against what the workflow can
absorb, not against what sounds impressive. And I'd measure the current
rate before promising anything; an SLO you're already violating isn't a
target, it's a fiction."

---

## Q6 — Versioning and promotion

**Thesis:** Pin the variant at session creation and never re-evaluate
mid-session.

### The answer

"Prompt, policy, and model id are three independently deployable config
artifacts, not code constants. My policy file carries a version number and
every audit row stamps it, so a denial from last Tuesday can be replayed
against the policy that was actually live at the time. The reason to keep
them separate is that a prompt rollback should be a config change measured
in seconds, not a container rebuild.

Promotion is a gate. Sandbox tier with synthetic data and loose policy,
where iteration is minutes. To reach production you need the policy file
reviewed, the eval suite passing at threshold, a red-team pass, and an
error budget that isn't already spent. Speed lives before the gate; rigour
lives at it. That's also the answer to 'don't guardrails slow us down' —
they slow down promotion, deliberately, and nothing else.

Canarying is where agents differ from normal services, and the hard part is
session affinity. You pin the variant at session creation and never
re-evaluate mid-session. A user whose turn three runs on a different prompt
than turns one and two gets incoherence — and worse, your eval numbers get
contaminated by mixed-variant sessions, so you can't even tell whether the
canary was better.

Ramp 1, 5, 25, 100, gated on the same three SLOs plus cost per turn.
Compare on task success and p95, not on eyeballing outputs.

For a model swap specifically I'd shadow first — same traffic to both, only
the incumbent's answer is served, diff them offline. It costs double for a
week and catches regressions no offline eval predicted."

### Detail to drop

> "Every audit row carries the policy version. That's not bookkeeping — it
> means when someone asks why a request was denied six weeks ago, I can
> answer against the policy that was live, not the one that's live now."

### If they push

**"How do you test non-deterministic software in CI?"**

"You split it. The deterministic parts get real assertions — my policy
engine has ten, the reliability primitives have twenty-three more, and the
whole suite runs in under a tenth of a second because none of it needs a
model. The non-deterministic part gets an eval suite with
a pass threshold and a variance check across repeated runs. The mistake is
treating the whole system as untestable because one component is
probabilistic. Most of an agent isn't."

---

## Q7 — Operability for SREs

**Thesis:** Every alert names an action and an impact.

### The answer

"The goal is that an SRE who's never read a tool implementation can still
resolve the page.

So every alert names an action and an expected impact. Not 'agent error
rate high' but 'request_signature breaker open for five minutes — check the
signature service, runbook section three, expected impact: signature sends
fail, reads unaffected.' I get that impact line for free, because blast
radius is already declared in the policy file. The alert can state impact
without the responder knowing what the tool does.

Liveness and readiness answer different questions. Liveness is just process
alive. Readiness is: model reachable, policy file parses, schema drift
check passes, breakers closed. A pod with an open breaker on a critical
tool should leave the rotation rather than quietly serving degraded
answers.

Self-healing that's already in the runtime rather than in a wiki: the
circuit breaker recovers without a human, the degradation ladder sheds
instead of falling over, bounded repair stops the token burn, and
default-deny means an unregistered tool fails closed rather than open.

The biggest toil reduction is making the audit log answer the most common
ticket. 'Why did it refuse for this customer' is the top question by volume,
and `rule=R-014` in a trace answers it without escalating to the team that
wrote the agent.

And three kill switches that are config rather than deploys: disable a
single tool, force a ladder tier, freeze promotion."

### Detail to drop

> "Default-deny is an operability feature as much as a security one. An
> unregistered tool fails closed with rule R-000 and a named reason, so the
> failure is legible. Fail-open would be a silent behaviour change nobody
> can debug."

### If they push

**"What's in the runbook?"**

"One section per alert, and each one has: what the user is experiencing
right now, the single command to confirm it, the mitigation that doesn't
require understanding the agent — usually flip a kill switch or force a
ladder tier — and the escalation path if that doesn't hold. If a runbook
section can't be executed by someone who's never seen the code, it isn't
finished."

---

## Q8 — The subtly incorrect result · BRING YOUR OWN

**Thesis:** Unit tests assert the tool returned 200. They cannot assert the
answer was right.

### Do not invent this one

An interviewer probes an incident story with "what did the graph look
like", "who found it", "how long was it live". A fabricated answer runs out
of detail in about two follow-ups, and the damage is worse than not having
the story.

If you have a real one, tell it in the shape below. If you don't:

> "I haven't shipped an agent at production scale long enough to have that
> story yet — the incidents I've had were loud, not subtle. What I'd watch
> for is..." then give the failure classes and the detection order. That
> answers honestly and still shows the reasoning, which is what they are
> actually grading.

### The shape

Situation → how it was found → why the tests missed it → what changed.
Spend most of the time on the last two.

### The failure classes worth naming

**Empty conflated with failed.** A tool returns an empty list and the agent
reports "no results found" when the truth was "the query errored". To a
model those are identical. Unit tests pass because the tool returned
`200 []`. This is the most common one and the most believable.

**Wrong-tenant or stale retrieval.** The answer is fluent, sourced, and
about the wrong contract.

**Silent truncation.** A long document is cut at the context limit and the
summary omits the clause that mattered. Nothing errors.

**A field that quietly vanished.** A schema change makes an optional field
always absent; the agent silently stops using it.

### How it actually gets caught, in order of value

1. **Sampled model-judge on real traffic**, scoring groundedness and task
   success. This is what makes task success an SLO rather than a wish.
2. **Canary questions** with known-correct answers, run continuously
   against production. A drop is unambiguous.
3. **Distribution monitoring** — tool-call mix, empty-result rate, answer
   length, refusal rate. The empty-result-rate jump is what catches class
   one, and it's a cheap metric.
4. **Business guardrails** — downstream correction rate, human override
   rate. Slow, but honest.
5. **User feedback**, which is where it usually actually comes from, weeks
   late. Say this part out loud; pretending otherwise reads as naive.

### What changes afterwards

The tool stops conflating empty with failed — distinct return shapes, and
the empty case gets its own metric. The case enters the eval suite as a
regression test. And the alert moves from error rate to output
distribution, because the error rate never moved.

---

## Q9 — Human-in-the-loop

**Thesis:** Keep it cheap by needing it rarely.

### The answer

"Blast radius decides who needs a human. Reads never do. Writes usually
don't. Outbound and destructive do. So the answer to 'without adding
unacceptable latency' isn't faster approvals — it's dramatically fewer of
them. The overwhelming majority of tool calls in a document workflow are
reads, and none of them touch a person.

It has to be asynchronous and durable. The approval must not hold a request
thread. The turn suspends to durable state and resumes on an event —
LangGraph's `interrupt`, or ADK's `LongRunningFunctionTool`. I'll be
straight that my current implementation routes correctly but doesn't
durably pause: the policy gate returns `approval_required` and the turn
ends. That's a known gap and it's on the backlog, not something I'd claim
is finished.

Avoiding the single point of failure means approver groups, never a named
individual. A timeout with an explicit default, and the default is deny for
anything outbound or destructive. Escalation to a second group after N
minutes. And a durable queue, so a redeploy doesn't drop pending
approvals."

### Detail to drop

The ordering trap, which you have a test for:

> "The trap is ordering. If the approval gate runs before the argument
> checks, humans become rubber stamps for calls that should have been
> denied outright. In my engine an unapproved counterparty domain is denied
> at rule fourteen and never reaches a person. Approval is for
> legitimate-but-consequential actions — it isn't a way to launder a policy
> violation through a tired person at 5pm on a Friday."

### If they push

**"Doesn't that just move the bottleneck to the approvers?"**

"It does, and that's why the routing rule matters more than the queue
design. If approvals are more than a few percent of calls, the blast-radius
classification is wrong, or the tool is too coarse and should be split into
a safe read and a gated write. The fix is upstream, not a bigger approver
rota."

---

## Q10 — Evaluating models beyond accuracy

**Thesis:** Evaluate per step, not per agent.

### The answer

"Five dimensions.

Quality, as task success judged on my traffic — not a public benchmark,
because the benchmark doesn't contain my documents. Latency at p50 and p95,
per turn rather than per call, because a turn is many calls and per-call
p95 becomes per-turn p99. Cost per turn including retries and repair
rounds, because a cheap model that needs two repairs isn't cheap.
Throughput, meaning tokens per second and my actual rate limit, which is
often the real constraint rather than price. And stability — variance
across identical runs, format adherence, and tool-call validity rate.

Stability is the one that gets skipped and then hurts. For a tool-using
agent, how reliably the model emits a well-formed tool call matters more
than raw reasoning quality, because every malformed call costs a repair
round — latency, tokens, and a chance of dropping through to the fallback.
A model that's three percent smarter and eight percent worse at schema
adherence is a net loss in an agent.

The trade-off I'd describe is that the split was by task, not by agent.
Retrieval and summarisation went to the cheap model with no measurable
task-success drop. Clause interpretation stayed on the strong one. Routing
by task type beat picking one model for everything.

The general principle is: evaluate per step, not per agent. An agent is
five different jobs wearing one name, and they don't all need the same
model."

### Detail to drop

> "Cost per turn, not per token, is the number that changed my mind once.
> A model with a lower per-token price that needed a second tool round and
> a repair was more expensive per completed task than the one that got it
> right first time."

**Substitute measured numbers before quoting the 12×/4× figures** — those
are the ladder's configured values in `degradation.py`, not something you
measured.

### If they push

**"How do you build the eval set?"**

"From production traffic, not from imagination. Sample real turns,
stratified by task type, and have the failures over-represented — a set
that's 95% easy cases can't distinguish two models. Then freeze it,
version it, and treat a change to the eval set as seriously as a change to
the code, because otherwise your score improves by editing the exam."

---

## Two things to say if you get stuck

**When you don't know:** "I haven't hit that in production. Here's how I'd
reason about it —" and then reason about it. Interviewers grade the
reasoning, and they can always tell the difference.

**When you're asked about a gap in your own work:** name it before they
find it. "That's a known gap — approval routing works, durable pause
doesn't, it's item one on my backlog." That reads as ownership. Discovering
it under questioning reads as something else.
