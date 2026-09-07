# 01 — Guardrail layers: the concepts

Reference notes for the interview question:

> In enterprise deployments, how do you balance the speed of agent
> development with the need for "policy as code" guardrails? Specifically,
> do you enforce guardrails at the model layer (e.g. Model Armor), at the
> agent orchestration layer, or both — and what trade-offs have you
> observed?

## What the question is testing

It is two questions in one coat. Part one is a management question: speed
versus control. Part two is an architecture question: where does the
control live. The interviewer wants to see that you know guardrails are
not one thing.

## Vocabulary

**Policy as code.** Your rules live in a versioned file, not a PDF or a
wiki page. "Only the legal group may call `send_for_signature`." "No
document from the HR repository may leave the tenant." The rule is text,
it sits in Git, it gets code review, and it runs in CI as a test. The
opposite is policy as documentation, where a human is supposed to
remember it.

**Model Armor.** Google Cloud's screening service for prompts and model
responses. You put it in front of and behind the model call. It looks for
prompt injection, jailbreak attempts, sensitive data, malicious URLs and
harmful content. AWS calls its version Bedrock Guardrails. Azure has
Content Safety. They all read text and score it.

**Agent orchestration layer.** Your framework or gateway. The part that
decides which tools exist, who may call them, what arguments are allowed,
how many steps to run, and when to ask a human.

## The request path

```
User request              carries identity and scopes
      |
Orchestration gate        identity, tool allowlist      <- deterministic
      |
Model-layer filter        injection, PII, harmful text   <- probabilistic
      |
Model                     plans and requests tools
      |
Tool-call gate            scope, blast radius, approval  <- deterministic
      |
Repository or API         identity-filtered results
```

## The core insight

The two layers stop different kinds of harm.

The model layer stops **content harm**. Bad text in, bad text out. Toxic
output, a leaked national ID number, a jailbreak prompt, an injected
instruction hidden in a scanned contract.

The orchestration layer stops **action harm**. The agent deleting 8,000
documents. The agent emailing a signed NDA to the wrong counterparty. The
agent reading a case file the user has no permission to see.

Only the second one can bankrupt you. A model filter cannot stop a delete.
It has no idea who the user is, what the tool does, or how many rows it
touches.

So the answer is "both", but not as a hedge. They are not two copies of
the same control. They are two different controls that people mistakenly
put in one bucket called "guardrails".

## What each layer is good and bad at

### Model layer

Good: cheap and uniform. Turn it on once and every agent gets it.
Model-agnostic, so swapping the underlying model does not break it.
Catches unknown-unknowns, because you did not have to predict the exact
attack.

Bad: probabilistic. False positives and false negatives. You cannot prove
to an auditor why it fired. No concept of identity, state or business
rules. It cannot know that this clause needs legal sign-off.

### Orchestration layer

Good: deterministic. Same input, same decision, every time. Testable in
CI. Produces a real decision log — subject, action, resource, allow or
deny, and which rule decided. Auditors love this. Understands identity
and business rules.

Bad: only catches known-knowns. You must write the rule before the rule
can protect you. Every new tool means new policy work, which is exactly
what slows teams down.

### The third layer people forget

The **data layer**. In a document-management setting, retrieval is a
security boundary. Repository ACLs must flow into the index and filter
results per user. If they do not, no filter above helps: the agent
becomes a very polite tool for reading other people's files.

## Trade-offs actually observed

**Over-blocking kills adoption.** Compliance and legal text triggers
content filters constantly. NDA and indemnity language is full of terms
that score as risky. Teams get filter fatigue, then they ask for an
exemption, and the exemption becomes permanent. Model-layer filters need
per-workflow tuning, not one global threshold.

**Latency compounds.** A filter on every prompt and every tool result, in
a loop of ten tool rounds, means twenty extra network calls. This is a
direct argument for fewer, batched tool rounds. Guardrail cost is per hop,
so reducing hops buys budget for stronger checks.

**Prompt injection in retrieved content is the hard one.** The untrusted
input is the document itself. A scanner helps, but it cannot be the
control, because you will never catch every phrasing. The real control is
architectural: retrieved text has no authority. It can inform an answer,
it can never authorise a tool call. Tool permissions come from the user's
identity, never from the model's intention.

**Explainability differs.** When a model filter refuses, you cannot tell
the customer why. When a policy engine denies, you can name the rule. In
regulated deployments the second is worth more than the first.

**Two layers means two debugging surfaces.** Something got blocked and
nobody knows which layer did it. You need one correlation ID across both,
or you spend your week on ticket archaeology.

## The speed half of the question

Honest framing: guardrails do not slow you down. Writing guardrails *per
agent* slows you down. So you move policy out of the agent and into the
platform.

**Make the fast path the safe path.** Agent teams get a gateway that
already has identity propagation, allowlisted tools, logging and step
limits. They build prompts and workflows. They never write auth code, so
they never get it wrong.

**Tier the ceremony by blast radius.** Read-only tools ship freely. Write
tools need a schema plus a policy rule. Irreversible or outbound tools
(delete, send, sign, publish) need human approval or a dry-run and diff.
Most tools are read-only, so most work stays fast. The ceremony lands only
where the damage would be.

**Separate prototype from production.** A sandbox tier with synthetic data
and loose policy, where iteration is hours. A promotion gate to production
that requires the policy file, an eval suite and a red-team pass. Speed
lives before the gate. Rigour lives at it.

## Spoken answer, about 60 seconds

Both layers, different jobs. Model layer for content risk, orchestration
layer for action risk. Model-layer filters are probabilistic and uniform,
so I use them as a net, not a control. Deterministic policy lives at the
orchestration and gateway layer, versioned in Git and tested in CI,
because that is what an auditor can read. Retrieval is filtered by the
user's own permissions, and retrieved content is never allowed to
authorise a tool call. On speed: policy sits in the platform, not in each
agent, and the amount of process scales with the blast radius of the tool.
The trade-offs I have watched are false-positive fatigue on legal text,
latency compounding across tool rounds, and the debugging cost of having
two places that can say no.
