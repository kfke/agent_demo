"""Reliability primitives for an agent runtime.

Zero imports from google.adk, for the same reason as policy.py: these have
to be assertable in CI without booting an agent, and portable if the
framework changes underneath them.

The load-bearing idea: retry safety is a property of the TOOL, not of the
error. Whether a failed call may be retried is read from the same
policy.yaml that decides who may call it, via blast_radius.
"""

from __future__ import annotations

import asyncio
import dataclasses
import time
import uuid
from typing import Any, Awaitable, Callable, Optional

# =============================================================
# Correlation -- one id per user turn, threaded through everything
# =============================================================


@dataclasses.dataclass
class Trace:
    """One id per user turn. Every layer stamps it on every row.

    In production this is an OTel trace_id and these are spans. The point
    is not the library, it is that the model layer, the policy layer and
    the tool all write the SAME id, so "which layer said no" is a filter
    rather than an archaeology project.
    """

    trace_id: str
    step: int = 0

    @classmethod
    def new(cls) -> "Trace":
        return cls(trace_id=f"tr-{uuid.uuid4().hex[:8]}")

    def span(self, name: str) -> str:
        self.step += 1
        return f"{self.trace_id}/{self.step:02d}-{name}"


class Telemetry:
    """Structured rows. Stand-in for an OTel exporter."""

    def __init__(self, echo: bool = True) -> None:
        self.rows: list[dict[str, Any]] = []
        self.echo = echo

    def emit(self, **row: Any) -> None:
        self.rows.append(row)
        if self.echo:
            kv = " ".join(f"{k}={v}" for k, v in row.items())
            print(f"    TRACE {kv}")

    def where(self, **match: Any) -> list[dict[str, Any]]:
        return [r for r in self.rows if all(r.get(k) == v for k, v in match.items())]


# =============================================================
# Deadline budget -- the fix for cascading latency
# =============================================================


class BudgetExhausted(Exception):
    """The turn ran out of time. Not a tool failure."""


@dataclasses.dataclass
class Budget:
    """A deadline for the WHOLE turn, not a timeout per call.

    Per-call timeouts multiply: 3 tool rounds x 3 retries x a 10s timeout
    is a 90s turn nobody budgeted for. A budget is absolute -- every
    downstream call gets what is LEFT, never a fresh 10s.
    """

    total_ms: float
    clock: Callable[[], float] = time.monotonic
    started: Optional[float] = None

    def __post_init__(self) -> None:
        if self.started is None:
            self.started = self.clock()

    @property
    def elapsed_ms(self) -> float:
        return (self.clock() - float(self.started)) * 1000.0

    @property
    def remaining_ms(self) -> float:
        return self.total_ms - self.elapsed_ms

    def check(self, label: str = "") -> None:
        if self.remaining_ms <= 0:
            raise BudgetExhausted(f"turn budget exhausted before {label or 'next call'}")

    def slice_ms(self, want_ms: float) -> float:
        """Never hand a downstream call more time than the turn has left."""
        return max(0.0, min(want_ms, self.remaining_ms))


# =============================================================
# Circuit breaker -- the fix for hammering a dead dependency
# =============================================================


class CircuitOpen(Exception):
    """Fail fast. The dependency is known-bad; do not spend the budget."""


class CircuitBreaker:
    """Per-dependency, never global. One sick tool must not trip the rest."""

    def __init__(
        self,
        name: str,
        failure_threshold: int = 3,
        reset_after_s: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.name = name
        self.failure_threshold = failure_threshold
        self.reset_after_s = reset_after_s
        self.clock = clock
        self.failures = 0
        self.opened_at: Optional[float] = None
        self.state = "closed"  # closed | open | half_open

    def allow(self) -> bool:
        if self.state == "open":
            if self.clock() - float(self.opened_at) >= self.reset_after_s:
                self.state = "half_open"  # let exactly one probe through
                return True
            return False
        return True

    def on_success(self) -> None:
        self.failures = 0
        self.state = "closed"
        self.opened_at = None

    def on_failure(self) -> None:
        self.failures += 1
        if self.state == "half_open" or self.failures >= self.failure_threshold:
            self.state = "open"
            self.opened_at = self.clock()


# =============================================================
# Idempotency -- the fix for duplicate side effects
# =============================================================


class IdempotencyStore:
    """Key -> first result. A replay returns the original, never re-sends.

    In production this is Redis or a table with a unique constraint, keyed
    on something the CALLER derives deterministically (doc id + action +
    turn id), never a random uuid generated at retry time.
    """

    def __init__(self) -> None:
        self._done: dict[str, Any] = {}

    def get(self, key: str) -> Optional[Any]:
        return self._done.get(key)

    def put(self, key: str, value: Any) -> None:
        self._done.setdefault(key, value)

    def __len__(self) -> int:
        return len(self._done)


# Retry safety is read off blast_radius, the same field policy.yaml uses
# to decide how much ceremony a tool needs.
RETRY_FREELY = {"read"}
RETRY_ONLY_WITH_KEY = {"write", "outbound"}
NEVER_RETRY = {"destructive"}


def retry_allowed(blast_radius: str, idem_key: Optional[str]) -> tuple[bool, str]:
    """Answers "may I try that again?" and says why not."""
    if blast_radius in NEVER_RETRY:
        return False, "destructive tools are never retried automatically"
    if blast_radius in RETRY_ONLY_WITH_KEY and not idem_key:
        return False, (
            f"a '{blast_radius}' tool with no idempotency key could duplicate "
            "the side effect"
        )
    return True, ""


# =============================================================
# The call wrapper that composes all four
# =============================================================


@dataclasses.dataclass
class CallResult:
    ok: bool
    value: Any = None
    error: str = ""
    attempts: int = 0
    replayed: bool = False
    stopped_by: str = ""  # budget | breaker | not_retryable | attempts


async def call_tool(
    fn: Callable[..., Awaitable[Any]],
    *,
    args: dict[str, Any],
    tool_name: str,
    blast_radius: str,
    budget: Budget,
    breaker: CircuitBreaker,
    telemetry: Telemetry,
    trace: Trace,
    store: IdempotencyStore,
    idem_key: Optional[str] = None,
    max_attempts: int = 3,
    per_call_ms: float = 300.0,
    base_backoff_ms: float = 20.0,
) -> CallResult:
    """One tool call with the four patterns applied in the right order.

    Order matters and is most of the answer to question 1:
      idempotency replay -> breaker -> budget -> attempt -> classify -> backoff
    """
    span = trace.span(tool_name)

    # 1. Replay before anything else. A retried turn must not re-send.
    if idem_key:
        cached = store.get(idem_key)
        if cached is not None:
            telemetry.emit(span=span, tool=tool_name, event="idempotent_replay",
                           key=idem_key)
            return CallResult(ok=True, value=cached, attempts=0, replayed=True)

    # 2. Fail fast on a known-bad dependency. Costs 0ms of the budget.
    if not breaker.allow():
        telemetry.emit(span=span, tool=tool_name, event="circuit_open",
                       state=breaker.state)
        return CallResult(ok=False, error="circuit_open", stopped_by="breaker")

    last_error = ""
    for attempt in range(1, max_attempts + 1):
        try:
            budget.check(tool_name)
        except BudgetExhausted as e:
            telemetry.emit(span=span, tool=tool_name, event="budget_exhausted",
                           attempt=attempt)
            return CallResult(ok=False, error=str(e), attempts=attempt - 1,
                              stopped_by="budget")

        timeout_s = budget.slice_ms(per_call_ms) / 1000.0
        try:
            value = await asyncio.wait_for(fn(**args), timeout=timeout_s)
        except asyncio.TimeoutError:
            last_error = f"timeout after {timeout_s * 1000:.0f}ms"
        except Exception as e:  # tool faults are data, not crashes
            last_error = f"{type(e).__name__}: {e}"
        else:
            breaker.on_success()
            if idem_key:
                store.put(idem_key, value)
            telemetry.emit(span=span, tool=tool_name, event="ok", attempt=attempt,
                           remaining_ms=round(budget.remaining_ms))
            return CallResult(ok=True, value=value, attempts=attempt)

        breaker.on_failure()
        telemetry.emit(span=span, tool=tool_name, event="attempt_failed",
                       attempt=attempt, error=last_error, breaker=breaker.state)

        # 3. May we try again at all? A property of the tool, not the error.
        may, why = retry_allowed(blast_radius, idem_key)
        if not may:
            telemetry.emit(span=span, tool=tool_name, event="retry_refused", reason=why)
            return CallResult(ok=False, error=last_error, attempts=attempt,
                              stopped_by="not_retryable")

        if attempt == max_attempts:
            break

        # 4. Backoff, but never past the deadline. A sleep you cannot
        #    afford is just a slower failure.
        backoff_ms = base_backoff_ms * (2 ** (attempt - 1))
        if backoff_ms > budget.remaining_ms:
            telemetry.emit(span=span, tool=tool_name,
                           event="backoff_would_exceed_budget",
                           backoff_ms=backoff_ms,
                           remaining_ms=round(budget.remaining_ms))
            return CallResult(ok=False, error=last_error, attempts=attempt,
                              stopped_by="budget")
        await asyncio.sleep(backoff_ms / 1000.0)

    return CallResult(ok=False, error=last_error, attempts=max_attempts,
                      stopped_by="attempts")
