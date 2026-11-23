"""The call timing rule. Pure functions only (no network, no clock) so it is easy to test.

remaining = deadline - processed_at

  remaining >= 12 h          -> call at deadline - 12 h
  2 h <= remaining < 12 h    -> call at deadline - 2 h
  1 h <= remaining < 2 h     -> call at deadline - 1 h
  0 < remaining < 1 h        -> call right away (processed_at)
  remaining <= 0             -> deadline passed: WhatsApp only, no call
  no deadline                -> call at processed_at + NO_DEADLINE_CALL_MINUTES
"""

from dataclasses import dataclass
from datetime import datetime, timedelta

# Reason codes, saved for logging and shown in dry runs
BEFORE_12H = "12h_before_deadline"
BEFORE_2H = "2h_before_deadline"
BEFORE_1H = "1h_before_deadline"
IMMEDIATE = "under_1h_left_call_now"
PASSED = "deadline_passed_no_call"
NO_DEADLINE = "no_deadline_call_later"


@dataclass(frozen=True)
class CallPlan:
    call_at: datetime | None  # None means: do not call
    reason: str

    @property
    def should_call(self) -> bool:
        return self.call_at is not None


def _check_aware(name: str, value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")


def compute_call_time(
    deadline: datetime | None,
    processed_at: datetime,
    no_deadline_call_minutes: int = 60,
) -> CallPlan:
    """Decide when (or whether) to call, based on how much time is left."""
    _check_aware("processed_at", processed_at)

    if deadline is None:
        return CallPlan(processed_at + timedelta(minutes=no_deadline_call_minutes), NO_DEADLINE)

    _check_aware("deadline", deadline)
    remaining = deadline - processed_at

    if remaining <= timedelta(0):
        return CallPlan(None, PASSED)
    if remaining < timedelta(hours=1):
        return CallPlan(processed_at, IMMEDIATE)
    if remaining < timedelta(hours=2):
        return CallPlan(deadline - timedelta(hours=1), BEFORE_1H)
    if remaining < timedelta(hours=12):
        return CallPlan(deadline - timedelta(hours=2), BEFORE_2H)
    return CallPlan(deadline - timedelta(hours=12), BEFORE_12H)


def is_call_due(call_at: datetime, now: datetime) -> bool:
    return now >= call_at
