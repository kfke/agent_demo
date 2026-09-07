"""Runnable answers to the Agentic Architecture questions.

  python reliability_demo.py           all scenarios
  python reliability_demo.py R3        one scenario

Each scenario prints what it proves. Blast radii are read from policy.yaml,
so the reliability layer and the guardrail layer share one governance
surface rather than disagreeing quietly.
"""

from __future__ import annotations

import asyncio
import sys
import time

from ndaguard.contracts import RepairLoop, SchemaRegistry, ToolSchema
from ndaguard.degradation import DEFAULT_SLOS, DegradationLadder, ErrorBudget, LoadShedder
from ndaguard.policy import PolicyEngine
from ndaguard.reliability import (
    Budget,
    CircuitBreaker,
    IdempotencyStore,
    Telemetry,
    Trace,
    call_tool,
)

POLICY = PolicyEngine()


def radius(tool_name: str) -> str:
    """One source of truth. policy.yaml already classified every tool."""
    return POLICY.doc["tools"][tool_name]["blast_radius"]


def head(title: str, question: str) -> None:
    print("\n" + "=" * 72)
    print(title)
    print(f"  answers: {question}")
    print("=" * 72)


def note(text: str) -> None:
    print(f"  >> {text}")


# =============================================================
# Fault injection
# =============================================================


class FlakyTool:
    """Fails the first `fail_times` calls, then succeeds."""

    def __init__(self, name: str, fail_times: int = 0, latency_ms: float = 10.0) -> None:
        self.name = name
        self.fail_times = fail_times
        self.latency_ms = latency_ms
        self.calls = 0

    async def __call__(self, **args):
        self.calls += 1
        await asyncio.sleep(self.latency_ms / 1000.0)
        if self.calls <= self.fail_times:
            raise ConnectionError(f"{self.name} upstream reset")
        return {"status": "ok", "tool": self.name, "args": args}


class SlowSender:
    """The realistic outbound failure: the SEND lands, the RESPONSE does not.

    Records the side effect, then stalls past the caller's timeout. From the
    caller's seat this is indistinguishable from "nothing happened", which
    is exactly why a naive retry duplicates it.

    `dedupe_key` models a downstream that honours an idempotency key --
    which is the only thing that makes the retry safe.
    """

    def __init__(self, stall_ms: float = 200.0) -> None:
        self.stall_ms = stall_ms
        self.sent: list[str] = []
        self.calls = 0

    async def __call__(self, doc_id: str, counterparty_domain: str,
                       dedupe_key: str | None = None):
        self.calls += 1
        if dedupe_key and dedupe_key in self.sent:
            return {"status": "already_sent", "doc_id": doc_id, "deduped": True}
        self.sent.append(dedupe_key or f"anon-{self.calls}")
        if self.calls == 1:
            await asyncio.sleep(self.stall_ms / 1000.0)  # caller times out here
        return {"status": "sent", "doc_id": doc_id, "to": counterparty_domain}


class DeadTool:
    async def __call__(self, **args):
        raise ConnectionRefusedError("signature service is down")


def kit(budget_ms: float = 2000.0, threshold: int = 3):
    return (
        Budget(total_ms=budget_ms),
        CircuitBreaker("dep", failure_threshold=threshold),
        Telemetry(),
        Trace.new(),
        IdempotencyStore(),
    )


# =============================================================
# R1  retry a read
# =============================================================


async def r1() -> None:
    head("R1. Retrying a READ tool", "Q1 - retries without duplicate side effects")
    tool = FlakyTool("search_ndas", fail_times=2)
    budget, breaker, tel, trace, store = kit()

    res = await call_tool(
        tool, args={"query": "acme"}, tool_name="search_ndas",
        blast_radius=radius("search_ndas"),
        budget=budget, breaker=breaker, telemetry=tel, trace=trace, store=store,
    )
    note(f"blast_radius=read -> retried freely. ok={res.ok} after {res.attempts} attempts")
    note(f"tool was invoked {tool.calls}x; a read has no side effect to duplicate")


# =============================================================
# R2  the outbound call you must NOT retry
# =============================================================


async def r2() -> None:
    head("R2. OUTBOUND with no idempotency key",
         "Q1 - why 'just add retries' is wrong")
    sender = SlowSender(stall_ms=200)
    budget, breaker, tel, trace, store = kit()

    res = await call_tool(
        sender, args={"doc_id": "NDA-0119", "counterparty_domain": "acme-corp.com"},
        tool_name="request_signature", blast_radius=radius("request_signature"),
        budget=budget, breaker=breaker, telemetry=tel, trace=trace, store=store,
        per_call_ms=60,  # times out while the send is in flight
    )
    note(f"caller saw: ok={res.ok} stopped_by={res.stopped_by}")
    note(f"but the document WAS sent: sender.sent = {sender.sent}")
    note("the timeout told us nothing about whether the side effect landed,")
    note("so the runtime refuses to retry rather than risk a second signature request")


# =============================================================
# R3  the same call, made retryable
# =============================================================


async def r3() -> None:
    head("R3. Same call with an idempotency key",
         "Q1 - the state that makes a retry safe")
    sender = SlowSender(stall_ms=200)
    budget, breaker, tel, trace, store = kit()
    key = "sig:NDA-0119:turn-7"  # derived from the turn, NOT random per attempt

    res = await call_tool(
        sender,
        args={"doc_id": "NDA-0119", "counterparty_domain": "acme-corp.com",
              "dedupe_key": key},
        tool_name="request_signature", blast_radius=radius("request_signature"),
        budget=budget, breaker=breaker, telemetry=tel, trace=trace, store=store,
        idem_key=key, per_call_ms=60,
    )
    note(f"ok={res.ok} after {res.attempts} attempts, result={res.value}")
    note(f"downstream saw {sender.calls} calls but recorded {len(sender.sent)} send: {sender.sent}")

    print()
    res2 = await call_tool(
        sender,
        args={"doc_id": "NDA-0119", "counterparty_domain": "acme-corp.com",
              "dedupe_key": key},
        tool_name="request_signature", blast_radius=radius("request_signature"),
        budget=budget, breaker=breaker, telemetry=tel, trace=trace, store=store,
        idem_key=key, per_call_ms=60,
    )
    note(f"a repeated turn replays from the store: replayed={res2.replayed}, "
         f"downstream calls still {sender.calls}")


# =============================================================
# R4  deadline budget
# =============================================================


async def r4() -> None:
    head("R4. Turn budget stops the cascade", "Q1 - cascading latency")
    tool = FlakyTool("redline_clause", fail_times=99, latency_ms=120)
    budget, breaker, tel, trace, store = kit(budget_ms=150)

    t0 = time.monotonic()
    res = await call_tool(
        tool, args={"doc_id": "X", "clause_count": 2}, tool_name="redline_clause",
        blast_radius=radius("redline_clause"),
        budget=budget, breaker=breaker, telemetry=tel, trace=trace, store=store,
        idem_key="rl:X:turn-9", base_backoff_ms=100,
    )
    took = (time.monotonic() - t0) * 1000
    note(f"stopped_by={res.stopped_by} after {res.attempts} attempt, {took:.0f}ms elapsed")
    note("the backoff was affordable in isolation and unaffordable in context;")
    note("the budget knows the difference, a per-call timeout does not")


# =============================================================
# R5  circuit breaker
# =============================================================


async def r5() -> None:
    head("R5. Circuit breaker", "Q1 - not hammering a dead dependency")
    dead = DeadTool()
    budget, breaker, tel, trace, store = kit(budget_ms=5000, threshold=3)

    t0 = time.monotonic()
    first = await call_tool(
        dead, args={"doc_id": "X", "counterparty_domain": "acme-corp.com"},
        tool_name="request_signature", blast_radius=radius("request_signature"),
        budget=budget, breaker=breaker, telemetry=tel, trace=trace, store=store,
        idem_key="sig:X:turn-1", base_backoff_ms=20,
    )
    t_first = (time.monotonic() - t0) * 1000

    print()
    t1 = time.monotonic()
    second = await call_tool(
        dead, args={"doc_id": "Y", "counterparty_domain": "acme-corp.com"},
        tool_name="request_signature", blast_radius=radius("request_signature"),
        budget=budget, breaker=breaker, telemetry=tel, trace=trace, store=store,
        idem_key="sig:Y:turn-2",
    )
    t_second = (time.monotonic() - t1) * 1000

    note(f"call 1: {first.attempts} attempts, {t_first:.0f}ms, breaker now '{breaker.state}'")
    note(f"call 2: stopped_by={second.stopped_by}, {t_second:.1f}ms - budget spent: none")
    note("the next user does not pay for the previous user's discovery that the tool is down")


# =============================================================
# R6  malformed tool calls and schema drift
# =============================================================


async def r6() -> None:
    head("R6. Malformed calls and schema drift",
         "Q3 - one bad output must not take down the pipeline")

    registry = SchemaRegistry([
        ToolSchema("search_ndas", 1, {"query": str}),
        ToolSchema("redline_clause", 2, {"doc_id": str, "clause_count": int}),
        ToolSchema("request_signature", 1, {"doc_id": str, "counterparty_domain": str},
                   {"dedupe_key": str}),
    ])
    loop = RepairLoop(registry, max_repairs=1)

    bad = {"doc_id": "NDA-0119", "clause_count": "seven"}
    print(f"  model emitted : {bad}")
    problems = registry.get("redline_clause").validate(bad)
    print(f"  validator     : {problems}")
    print("  sent back     :")
    for line in RepairLoop.repair_prompt("redline_clause", problems).splitlines():
        print(f"      {line}")

    out = loop.run("redline_clause", [bad, {"doc_id": "NDA-0119", "clause_count": 7}])
    note(f"outcome={out.status} in {out.attempts} tries -> {out.args}")

    print()
    hopeless = loop.run("redline_clause", [bad, {"doc_id": "NDA-0119"}, {"nope": 1}])
    note(f"model that cannot satisfy the schema: status={hopeless.status} "
         f"after {hopeless.attempts} tries (bounded, not infinite)")
    note(f"last problems: {hopeless.problems}")

    print()
    pinned = {"search_ndas": 1, "redline_clause": 1, "purge_document": 1}
    note("boot-time drift check against the versions this build was tested on:")
    for d in registry.drift_against(pinned):
        print(f"      DRIFT {d}")
    note("this fires in a readiness probe, so the rollout stops before a user sees it")


# =============================================================
# R7  throttling, degradation, SLOs
# =============================================================


async def r7() -> None:
    head("R7. Provider throttling and graceful degradation",
         "Q5 - SLOs, backpressure, what breaks first")

    print("  SLOs:")
    for s in DEFAULT_SLOS:
        print(f"      {s.name:<24} target {s.target:.3f} over {s.window_days}d "
              f"-> budget {s.budget_for(1_000_000):,.0f} bad events / 1M")

    ladder = DegradationLadder()
    budget = ErrorBudget(total=100)

    print()
    print(f"  {'throttle':>9}  {'budget':>7}  {'tier':<9} {'model':<18} {'cost':>5} "
          f"{'p95':>7}  serves")
    for throttle, burn, cache in [(0.00, 0, False), (0.15, 10, False),
                                  (0.50, 30, True), (0.50, 30, False),
                                  (0.80, 100, True)]:
        budget.spent = burn
        t = ladder.pick(throttle_rate=throttle, budget_remaining=budget.remaining_fraction,
                        cache_hit=cache)
        print(f"  {throttle:>9.0%}  {budget.remaining_fraction:>7.0%}  {t.name:<9} "
              f"{t.model:<18} {t.rel_cost:>5.2f} {t.p95_ms:>6}ms  {t.serves}")

    print()
    shed = LoadShedder(max_in_flight=4, max_queued=2)
    admitted = sum(1 for _ in range(10) if shed.admit())
    note(f"admission control: 10 arrivals -> {admitted} admitted, {shed.rejected} rejected at the door")
    note("rejecting fast beats accepting work the client will abandon anyway")
    note("the page fires on error-budget BURN RATE, not on a single 429")


# =============================================================
# R8  correlation
# =============================================================


async def r8() -> None:
    head("R8. One trace id across every layer",
         "Q4 - correlating logs, traces and metrics")

    budget, breaker, tel, trace, store = kit()
    print(f"  turn trace_id: {trace.trace_id}")
    print()

    await call_tool(FlakyTool("search_ndas"), args={"query": "acme"},
                    tool_name="search_ndas", blast_radius=radius("search_ndas"),
                    budget=budget, breaker=breaker, telemetry=tel, trace=trace, store=store)
    await call_tool(FlakyTool("read_clause", fail_times=1),
                    args={"doc_id": "NDA-0442", "repository": "tenant-eu"},
                    tool_name="read_clause", blast_radius=radius("read_clause"),
                    budget=budget, breaker=breaker, telemetry=tel, trace=trace, store=store)

    # the guardrail layer stamps the same id
    decision = POLICY.evaluate(role="legal_counsel", tool_name="request_signature",
                               args={"doc_id": "NDA-0442",
                                     "counterparty_domain": "data-exfil.example"},
                               session={"tenant": "tenant-eu"})
    tel.emit(span=trace.span("policy_gate"), layer="orchestration",
             event=decision.effect, rule=decision.rule_id)

    print()
    note(f"{len(tel.rows)} spans, all under {trace.trace_id}")
    note("a user says 'it refused at about 14:32'. You filter on the trace id and")
    note(f"the answer is one row: rule={decision.rule_id}")
    note("without the shared id this is three log searches and a guess")


# =============================================================

SCENARIOS = {"R1": r1, "R2": r2, "R3": r3, "R4": r4,
             "R5": r5, "R6": r6, "R7": r7, "R8": r8}


async def main() -> None:
    picked = [a.upper() for a in sys.argv[1:] if a.upper() in SCENARIOS]
    for key in picked or SCENARIOS:
        await SCENARIOS[key]()
    print("\n" + "-" * 72)
    print("Q1 -> R1 R2 R3 R4 R5   Q3 -> R6   Q4 -> R8   Q5 -> R7")
    print("Q2 Q6 Q7 Q8 Q9 Q10 are narrative: see docs/04-reliability-patterns.md")


if __name__ == "__main__":
    asyncio.run(main())
