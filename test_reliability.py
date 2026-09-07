"""Reliability tests. Run: python -m pytest test_reliability.py -q

Same argument as test_policy.py: these patterns are deterministic, so they
belong in CI as assertions rather than in a doc as claims.

Async cases use asyncio.run inside a sync test so the suite needs no
pytest-asyncio.
"""

import asyncio

from ndaguard.contracts import RepairLoop, SchemaRegistry, ToolSchema
from ndaguard.degradation import DegradationLadder, ErrorBudget, LoadShedder
from ndaguard.policy import PolicyEngine
from ndaguard.reliability import (
    NEVER_RETRY,
    RETRY_FREELY,
    RETRY_ONLY_WITH_KEY,
    Budget,
    BudgetExhausted,
    CircuitBreaker,
    IdempotencyStore,
    Telemetry,
    Trace,
    call_tool,
    retry_allowed,
)


def kit(budget_ms=2000.0, threshold=3):
    return (Budget(total_ms=budget_ms), CircuitBreaker("t", failure_threshold=threshold),
            Telemetry(echo=False), Trace.new(), IdempotencyStore())


# ---- retry safety is a property of the tool ------------------


def test_reads_retry_freely():
    assert retry_allowed("read", None)[0]


def test_outbound_without_key_is_not_retryable():
    ok, why = retry_allowed("outbound", None)
    assert not ok and "idempotency key" in why


def test_outbound_with_key_is_retryable():
    assert retry_allowed("outbound", "sig:X:turn-1")[0]


def test_destructive_is_never_retryable_even_with_a_key():
    assert not retry_allowed("destructive", "any-key")[0]


def test_every_policy_tool_has_a_retry_class():
    """Cross-file invariant: policy.yaml and reliability.py must agree.

    Adding a tool with a new blast_radius fails here rather than silently
    getting default retry behaviour.
    """
    classes = RETRY_FREELY | RETRY_ONLY_WITH_KEY | NEVER_RETRY
    for name, spec in PolicyEngine().doc["tools"].items():
        assert spec["blast_radius"] in classes, name


# ---- budget --------------------------------------------------


def test_budget_never_hands_out_more_time_than_it_has():
    b = Budget(total_ms=50)
    assert b.slice_ms(5000) <= 50


def test_exhausted_budget_raises():
    b = Budget(total_ms=0)
    try:
        b.check("next_tool")
    except BudgetExhausted:
        return
    raise AssertionError("expected BudgetExhausted")


# ---- circuit breaker -----------------------------------------


def test_breaker_opens_at_threshold():
    cb = CircuitBreaker("dep", failure_threshold=3)
    for _ in range(3):
        cb.on_failure()
    assert cb.state == "open" and not cb.allow()


def test_breaker_half_opens_then_reopens_on_a_failed_probe():
    clock = [0.0]
    cb = CircuitBreaker("dep", failure_threshold=1, reset_after_s=10,
                        clock=lambda: clock[0])
    cb.on_failure()
    assert not cb.allow()
    clock[0] = 11.0
    assert cb.allow() and cb.state == "half_open"
    cb.on_failure()
    assert cb.state == "open"


def test_breaker_is_per_dependency():
    a, b = CircuitBreaker("a", 1), CircuitBreaker("b", 1)
    a.on_failure()
    assert not a.allow() and b.allow()


# ---- idempotency ---------------------------------------------


def test_store_is_first_write_wins():
    s = IdempotencyStore()
    s.put("k", "first")
    s.put("k", "second")
    assert s.get("k") == "first"


def test_replay_does_not_invoke_the_tool():
    calls = []

    async def tool(**kw):
        calls.append(kw)
        return "sent"

    async def go():
        budget, breaker, tel, trace, store = kit()
        common = dict(args={"doc_id": "X"}, tool_name="request_signature",
                      blast_radius="outbound", budget=budget, breaker=breaker,
                      telemetry=tel, trace=trace, store=store, idem_key="k1")
        await call_tool(tool, **common)
        return await call_tool(tool, **common)

    second = asyncio.run(go())
    assert second.replayed and second.value == "sent" and len(calls) == 1


def test_outbound_failure_without_key_stops_after_one_attempt():
    async def boom(**kw):
        raise ConnectionError("down")

    async def go():
        budget, breaker, tel, trace, store = kit()
        return await call_tool(boom, args={}, tool_name="request_signature",
                               blast_radius="outbound", budget=budget, breaker=breaker,
                               telemetry=tel, trace=trace, store=store)

    r = asyncio.run(go())
    assert r.attempts == 1 and r.stopped_by == "not_retryable"


def test_backoff_that_exceeds_the_budget_stops_the_call():
    async def boom(**kw):
        raise ConnectionError("down")

    async def go():
        budget, breaker, tel, trace, store = kit(budget_ms=30)
        return await call_tool(boom, args={}, tool_name="search_ndas",
                               blast_radius="read", budget=budget, breaker=breaker,
                               telemetry=tel, trace=trace, store=store,
                               base_backoff_ms=500)

    assert asyncio.run(go()).stopped_by == "budget"


# ---- contracts -----------------------------------------------


REG = SchemaRegistry([
    ToolSchema("redline_clause", 2, {"doc_id": str, "clause_count": int}),
])


def test_bool_is_not_accepted_as_int():
    assert REG.get("redline_clause").validate(
        {"doc_id": "X", "clause_count": True}) == ["argument 'clause_count' must be int, got bool"]


def test_unknown_argument_is_a_problem():
    problems = REG.get("redline_clause").validate(
        {"doc_id": "X", "clause_count": 1, "extra": "?"})
    assert any("unknown argument" in p for p in problems)


def test_repair_is_bounded_and_falls_back():
    loop = RepairLoop(REG, max_repairs=1)
    out = loop.run("redline_clause", [{"doc_id": "X"}, {"doc_id": "X"}, {"doc_id": "X"}])
    assert out.status == "fallback" and out.attempts == 2


def test_repair_succeeds_on_the_second_proposal():
    loop = RepairLoop(REG, max_repairs=1)
    out = loop.run("redline_clause",
                   [{"doc_id": "X", "clause_count": "seven"},
                    {"doc_id": "X", "clause_count": 7}])
    assert out.status == "repaired" and out.args["clause_count"] == 7


def test_schema_drift_is_detected():
    drift = REG.drift_against({"redline_clause": 1})
    assert drift and "v1" in drift[0] and "v2" in drift[0]


# ---- degradation ---------------------------------------------


def test_exhausted_error_budget_refuses_regardless_of_throttle():
    t = DegradationLadder().pick(throttle_rate=0.0, budget_remaining=0.0)
    assert t.name == "refuse"


def test_ladder_descends_with_throttle_pressure():
    ladder = DegradationLadder()
    names = [ladder.pick(throttle_rate=r, budget_remaining=1.0, cache_hit=True).name
             for r in (0.0, 0.2, 0.6)]
    assert names == ["primary", "cheap", "cached"]


def test_error_budget_never_goes_negative():
    b = ErrorBudget(total=10)
    b.burn(50)
    assert b.remaining == 0.0 and b.remaining_fraction == 0.0


def test_shedder_rejects_beyond_capacity():
    s = LoadShedder(max_in_flight=2, max_queued=1)
    assert [s.admit() for _ in range(5)] == [True, True, True, False, False]
    assert s.rejected == 2
