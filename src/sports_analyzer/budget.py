"""The Odds API credit budget: spend evenly so the page never goes dark mid-month.

The free plan gives 500 credits per quota period. Every run that buys odds asks
`allowance_today()` how many credits it may spend today and buys less (h2h only)
or nothing (the previous prices are kept, marked stale) when the allowance is short.

The quota period is not documented for the free plan (calendar month or 30 days
from sign-up), so the budget assumes the longer one: 30 days from the first day
it saw this period's counter. A drop in `x-requests-used` means a new period began;
the budget notices that and starts over. Assuming the longer period only ever
makes it spend more slowly, never run out early.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from pathlib import Path

log = logging.getLogger(__name__)

PERIOD_DAYS = 30
# Kept back for the evening refresh after a heavy day and for manual runs.
RESERVE = 25
# A day may spend up to this multiple of its even share: weekends need more than
# midweek, and tomorrow's share shrinks by whatever today overspent.
BURST = 1.6


@dataclass(frozen=True)
class BudgetState:
    period_start: str  # ISO date the current quota period was first seen
    used: int | None = None  # x-requests-used after the last run
    remaining: int | None = None  # x-requests-remaining after the last run
    day: str = ""  # UTC date of `day_start_used`
    day_start_used: int | None = None  # `used` before the first purchase of that day


def load(path: Path, today: date) -> BudgetState:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return BudgetState(**{k: raw.get(k) for k in BudgetState.__dataclass_fields__})
    except (OSError, ValueError, TypeError):
        return BudgetState(period_start=today.isoformat())


def save(path: Path, state: BudgetState) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(state), indent=1) + "\n", encoding="utf-8")


def allowance_today(state: BudgetState, today: date) -> float | None:
    """Credits this run may still spend today; None when the counter has never been seen."""
    if state.remaining is None:
        return None
    spent_today = 0
    if (
        state.day == today.isoformat()
        and state.used is not None
        and state.day_start_used is not None
    ):
        spent_today = max(0, state.used - state.day_start_used)
    return max(0.0, BURST * daily_share(state, today, spent_today) - spent_today)


def daily_share(state: BudgetState, today: date, spent_today: int = 0) -> float:
    """An even split of the credits left (above the reserve) over the days left."""
    if state.remaining is None:
        return 0.0
    end = date.fromisoformat(state.period_start) + timedelta(days=PERIOD_DAYS)
    days_left = max(1, (end - today).days)
    # Counted from the start of today, so a run late in the day sees the same share.
    return max(0.0, (state.remaining + spent_today - RESERVE) / days_left)


def record(state: BudgetState, used: int | None, remaining: int | None, today: date) -> BudgetState:
    """Fold a run's final counters into the state (detects a quota reset)."""
    if used is None or remaining is None:
        return state
    period_start = state.period_start
    if state.used is not None and used < state.used:
        period_start = today.isoformat()
        log.info("odds budget: quota reset detected (used %s -> %s)", state.used, used)
    if state.day != today.isoformat():
        # The previous run's counter is where today started.
        day, day_start = today.isoformat(), state.used if state.used is not None else used
        if period_start == today.isoformat() and state.used is not None and used < state.used:
            day_start = 0
    else:
        day, day_start = state.day, state.day_start_used
    return BudgetState(period_start, used, remaining, day, day_start)


def plan_markets(
    wanted: tuple[str, ...], calls: int, allowance: float | None, essential: tuple[str, ...]
) -> tuple[str, ...]:
    """The markets this run can afford: all wanted, the essential ones, or none.

    `calls` is how many odds requests the run will make; each costs len(markets).
    """
    if allowance is None or calls == 0:
        return wanted
    if calls * len(wanted) <= allowance:
        return wanted
    if essential and calls * len(essential) <= allowance:
        log.warning("odds budget: %.1f credits left today; buying %s only", allowance, essential)
        return essential
    log.warning("odds budget: %.1f credits left today; keeping earlier prices", allowance)
    return ()
