"""Graceful degradation under provider throttling, driven by an error budget.

Answers question 5. The provider starts returning 429s. You have three
honest options and one dishonest one:

  honest:    serve a cheaper answer, serve a cached answer, refuse quickly
  dishonest: queue everything and let p99 go to 40 seconds

The ladder below picks deliberately instead of letting the queue decide.

Zero google.adk imports, same reason as policy.py.
"""

from __future__ import annotations

import dataclasses
from typing import Optional

# =============================================================
# SLOs and the error budget
# =============================================================


@dataclasses.dataclass
class SLO:
    """What you promised. Agents need three, not one.

    Availability alone hides the failure mode that actually annoys users:
    the agent answered, on time, with something useless. `task_success` is
    measured from an offline judge over sampled traffic, not from HTTP 200s.
    """

    name: str
    target: float          # e.g. 0.99
    window_days: int = 28

    def budget_for(self, events: int) -> float:
        """How many failures the window can absorb."""
        return events * (1.0 - self.target)


DEFAULT_SLOS = [
    SLO("availability", 0.995),          # turn completed without a 5xx
    SLO("latency_p95_under_8s", 0.95),   # a turn is many calls; p95 is the honest one
    SLO("task_success", 0.90),           # judged offline on sampled traffic
]


@dataclasses.dataclass
class ErrorBudget:
    """Burn tracking. Drives the ladder, and drives whether you may deploy."""

    total: float
    spent: float = 0.0

    def burn(self, n: float = 1.0) -> None:
        self.spent += n

    @property
    def remaining(self) -> float:
        return max(0.0, self.total - self.spent)

    @property
    def remaining_fraction(self) -> float:
        return self.remaining / self.total if self.total else 0.0


# =============================================================
# The ladder
# =============================================================


@dataclasses.dataclass(frozen=True)
class Tier:
    """One rung. Cheaper and dumber as you descend."""

    name: str
    model: str
    rel_cost: float          # per turn, relative to the primary
    p95_ms: int
    serves: str              # what the user actually gets


LADDER = [
    Tier("primary",   "gemini-2.5-pro",   1.00, 4200, "full tool-using answer"),
    Tier("cheap",     "gemini-2.5-flash", 0.08, 1100, "full tool-using answer, weaker reasoning"),
    Tier("cached",    "none",             0.00,   30, "last known good answer for this query shape"),
    Tier("refuse",    "none",             0.00,    5, "an honest 'try again in a minute'"),
]


class DegradationLadder:
    """Picks a rung from throttle pressure and remaining error budget.

    The rule that matters: shed load at the EDGE, before the expensive
    call, not by timing out after you have already paid for it.
    """

    def __init__(self, tiers: Optional[list[Tier]] = None) -> None:
        self.tiers = tiers or LADDER

    def pick(
        self,
        *,
        throttle_rate: float,        # fraction of recent calls returning 429
        budget_remaining: float,     # 1.0 = untouched, 0.0 = exhausted
        cache_hit: bool = False,
    ) -> Tier:
        by_name = {t.name: t for t in self.tiers}

        # Budget gone: stop making promises you cannot keep.
        if budget_remaining <= 0.0:
            return by_name["refuse"]

        if throttle_rate < 0.10:
            return by_name["primary"]

        if throttle_rate < 0.40:
            return by_name["cheap"]

        if cache_hit:
            return by_name["cached"]

        return by_name["refuse"]


# =============================================================
# Admission control
# =============================================================


class LoadShedder:
    """Bounded concurrency with a bounded queue.

    An unbounded queue converts a throughput problem into a latency problem
    and then into an OOM. Rejecting at the door is kinder than accepting
    work you will not finish before the client gives up.
    """

    def __init__(self, max_in_flight: int, max_queued: int) -> None:
        self.max_in_flight = max_in_flight
        self.max_queued = max_queued
        self.in_flight = 0
        self.queued = 0
        self.rejected = 0

    def admit(self) -> bool:
        if self.in_flight < self.max_in_flight:
            self.in_flight += 1
            return True
        if self.queued < self.max_queued:
            self.queued += 1
            return True
        self.rejected += 1
        return False

    def release(self) -> None:
        if self.queued:
            self.queued -= 1
        elif self.in_flight:
            self.in_flight -= 1
