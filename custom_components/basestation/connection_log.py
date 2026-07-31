"""
In-memory record of what the BLE layer has actually been doing.

Home Assistant defaults to INFO logging, where nearly every BLE failure path in this integration
is invisible - and by the time a stuck ESPHome proxy slot gets noticed (typically hours later),
any DEBUG logs that would have explained it have long since rotated away. This keeps a small
rolling window of connection events plus lifetime counters in memory instead, so `diagnostics.py`
can hand over the run-up to a lockup even though nobody had DEBUG logging enabled in advance.

Deliberately stdlib-only, with no Home Assistant or bleak imports, so that what it reports stays
independent of the connection code it observes.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import asdict, dataclass, replace
from enum import StrEnum
from typing import Any

# Only noteworthy events are retained (see `store` in ConnectionLog.record), so a healthy device
# adds nothing here for days at a time and this covers a long history of whatever did go wrong.
# Were routine polling stored too, 50 slots would be exhausted inside half an hour at the default
# 60s interval - and the events worth having would be the ones pushed out.
MAX_EVENTS = 50

# Longer messages are almost always a bleak traceback repr whose tail adds nothing.
MAX_DETAIL_LENGTH = 200

# How far back _append looks for an identical event to collapse into. Comfortably wider than the
# handful of events one failing poll cycle emits, so a repeating cycle collapses instead of
# churning - but narrow enough that a failure recurring much later still reads as a new entry.
COLLAPSE_WINDOW = 8


class Phase(StrEnum):
    """Which part of a BLE exchange an event belongs to."""

    CONNECT = "connect"
    OPERATION = "operation"
    DISCONNECT = "disconnect"
    LOCK = "lock"
    AVAILABILITY = "availability"


class Outcome(StrEnum):
    """How that part of the exchange ended."""

    OK = "ok"
    ERROR = "error"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    OUT_OF_SLOTS = "out_of_slots"
    # Our wait for a connect elapsed; the attempt itself was left running (see
    # BasestationDevice._await_connection).
    ABANDONED = "abandoned"
    # An abandoned connect eventually succeeded and we closed it again, so its slot went back.
    RECLAIMED = "reclaimed"
    # A connection we could not confirm was closed - the shape of failure that leaks a proxy slot.
    STRANDED = "stranded"
    UNAVAILABLE = "unavailable"
    AVAILABLE = "available"


#: Outcomes worth remembering as "the last thing that went wrong".
FAILURE_OUTCOMES = frozenset(
    {
        Outcome.ERROR,
        Outcome.TIMEOUT,
        Outcome.CANCELLED,
        Outcome.OUT_OF_SLOTS,
        Outcome.STRANDED,
    }
)


@dataclass(frozen=True, slots=True)
class ConnectionEvent:
    """A single thing that happened on the BLE path, with enough context to interpret it later."""

    at: float
    """Wall-clock (`time.time()`) timestamp, so it can be lined up against Home Assistant's log."""

    phase: Phase
    outcome: Outcome
    detail: str | None = None
    duration: float | None = None

    count: int = 1
    """How many identical events this represents (see ConnectionLog._append)."""

    first_at: float | None = None
    """When the first of a collapsed run happened; None while count is 1."""

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable form for diagnostics."""
        data: dict[str, Any] = {
            "at": self.at,
            "phase": str(self.phase),
            "outcome": str(self.outcome),
        }
        if self.duration is not None:
            data["duration"] = round(self.duration, 3)
        if self.detail is not None:
            data["detail"] = self.detail
        if self.count > 1:
            data["count"] = self.count
            data["first_at"] = self.first_at
        return data


@dataclass
class ConnectionStats:
    """
    Lifetime counters for one device, reset only when the config entry reloads.

    These exist to answer questions that a snapshot of current state cannot: whether a station
    that is unavailable *now* has been failing steadily for hours or fell over a minute ago, and
    whether the integration has been quietly leaking proxy slots the whole time.
    """

    connect_started: int = 0
    connect_succeeded: int = 0
    connect_failed: int = 0
    connect_reused: int = 0
    connect_abandoned: int = 0
    abandoned_reclaimed: int = 0
    abandoned_close_failed: int = 0
    disconnect_failed: int = 0
    disconnect_cancelled: int = 0
    out_of_slots: int = 0
    lock_timeout: int = 0
    operation_succeeded: int = 0
    operation_failed: int = 0
    became_unavailable: int = 0

    @property
    def suspected_stranded_slots(self) -> int:
        """
        Count events that can plausibly have left an ESPHome proxy slot occupied.

        A connection torn down mid-disconnect, or one established successfully that then could not
        be closed again, is exactly the shape of failure behind the proxy slot leak this
        integration has been chasing. This is the number to correlate against a station going
        unavailable: if it climbs at the moment things break, the disconnect path is implicated;
        if it stays at zero, the cause is elsewhere.
        """
        return self.disconnect_cancelled + self.abandoned_close_failed

    @property
    def failures_total(self) -> int:
        """Return every failed connect or operation, for a single at-a-glance health number."""
        return self.connect_failed + self.operation_failed

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable form for diagnostics, derived values included."""
        return {
            **asdict(self),
            "failures_total": self.failures_total,
            "suspected_stranded_slots": self.suspected_stranded_slots,
        }


class ConnectionLog:
    """Rolling event history plus lifetime counters for a single device."""

    def __init__(self, max_events: int = MAX_EVENTS) -> None:
        """Initialize an empty log."""
        self.stats = ConnectionStats()
        self.last_failure: ConnectionEvent | None = None
        self.last_success: ConnectionEvent | None = None
        self._events: deque[ConnectionEvent] = deque(maxlen=max_events)

    def record(
        self,
        phase: Phase,
        outcome: Outcome,
        *,
        detail: str | None = None,
        duration: float | None = None,
        store: bool = True,
    ) -> ConnectionEvent:
        """
        Note an event, tracking it as the latest failure/success and optionally keeping it.

        Pass `store=False` for the routine happy path - a poll that connected, read and
        disconnected exactly as intended. Those repeat every scan interval, and keeping them would
        evict the handful of events that actually explain a failure long before anyone goes
        looking. Their volume is already captured by the counters, and the most recent one is
        still retained as `last_success`, which is what makes "it was fine until 04:12" answerable.
        """
        if detail is not None and len(detail) > MAX_DETAIL_LENGTH:
            detail = detail[:MAX_DETAIL_LENGTH] + "..."

        event = ConnectionEvent(at=time.time(), phase=phase, outcome=outcome, detail=detail, duration=duration)
        if store:
            self._append(event)

        if outcome in FAILURE_OUTCOMES:
            self.last_failure = event
        elif outcome is Outcome.OK:
            self.last_success = event

        return event

    def record_exception(
        self,
        phase: Phase,
        outcome: Outcome,
        err: BaseException,
        *,
        context: str | None = None,
        duration: float | None = None,
    ) -> ConnectionEvent:
        """
        Record an event whose detail is an exception.

        The exception type is kept alongside its message because it's what actually classifies the
        failure - `BleakOutOfConnectionSlotsError` and a plain `TimeoutError` read almost the same
        by message alone but mean very different things about the proxy.
        """
        detail = f"{type(err).__name__}: {str(err) or 'no message'}"
        if context:
            detail = f"{detail} ({context})"
        return self.record(phase, outcome, detail=detail, duration=duration)

    def _append(self, event: ConnectionEvent) -> None:
        """
        Add an event, collapsing it into a recent matching one if it is a repeat.

        An outage doesn't produce one interesting event, it produces the same events once per poll
        for as long as it lasts. Without collapsing, a station down for an hour pushes everything
        earlier out of the buffer - including the first failure and the availability change, which
        are the two that actually explain what happened.

        The match is looked for across a short window rather than only against the immediately
        previous event, because one failing poll emits several events in a fixed cycle (a
        cancelled connect wait, then the timeout it surfaces as). Those interleave, so a
        consecutive-only check would never collapse anything and the buffer would still churn.
        Matching within the window keeps each distinct kind of failure to a single entry, in the
        order it was first seen, carrying how often it has repeated and when it started.
        """
        signature = (event.phase, event.outcome, event.detail)

        for offset in range(1, min(COLLAPSE_WINDOW, len(self._events)) + 1):
            previous = self._events[-offset]
            if (previous.phase, previous.outcome, previous.detail) == signature:
                self._events[-offset] = replace(
                    event,
                    count=previous.count + 1,
                    first_at=previous.first_at if previous.first_at is not None else previous.at,
                )
                return

        self._events.append(event)

    @property
    def events(self) -> list[ConnectionEvent]:
        """Return the retained events, oldest first."""
        return list(self._events)

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable summary for diagnostics."""
        return {
            "stats": self.stats.as_dict(),
            "last_failure": self.last_failure.as_dict() if self.last_failure else None,
            "last_success": self.last_success.as_dict() if self.last_success else None,
            "events": [event.as_dict() for event in self._events],
        }
