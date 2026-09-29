from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from threading import Lock


@dataclass
class ActionRecord:
    timestamp: datetime
    machine_key: str  # or "cluster@<key>" for a cluster
    action: str  # "on" | "off"
    source: str  # "schedule" | "manual" | "watchdog" | "cluster:<key>"
    result: str  # "ok" | "skipped" | "error"; clusters also "started" | "cancelled"
    detail: str = ""


class EventLog:
    """In-memory log: one global list for the overview, plus one per machine/cluster.
    Log records are routed to owners via logging's extra={"owners": [...]}."""

    def __init__(self, max_events: int = 200, max_per_owner: int = 50):
        self._lock = Lock()
        self._events: deque[str] = deque(maxlen=max_events)
        self._owner_events: dict[str, deque[str]] = {}
        self._max_per_owner = max_per_owner
        self._last_actions: dict[str, ActionRecord] = {}

    def add_event(self, message: str, owners: list[str] | tuple[str, ...] = ()) -> None:
        line = f"{datetime.now():%Y-%m-%d %H:%M:%S}  {message}"
        with self._lock:
            self._events.append(line)
            for owner in owners:
                self._owner_events.setdefault(owner, deque(maxlen=self._max_per_owner)).append(line)

    def record_action(self, machine_key: str, action: str, source: str, result: str, detail: str = "") -> None:
        with self._lock:
            self._last_actions[machine_key] = ActionRecord(datetime.now(), machine_key, action, source, result, detail)

    def last_action_for(self, machine_key: str) -> ActionRecord | None:
        with self._lock:
            return self._last_actions.get(machine_key)

    def recent_events(self, limit: int = 20) -> list[str]:
        with self._lock:
            return list(self._events)[-limit:][::-1]

    def recent_events_for(self, owner: str, limit: int = 30) -> list[str]:
        with self._lock:
            return list(self._owner_events.get(owner, ()))[-limit:][::-1]

    def forget(self, owner: str) -> None:
        """Drop a deleted machine's/cluster's history, so a new one reusing the key starts clean."""
        with self._lock:
            self._owner_events.pop(owner, None)
            self._last_actions.pop(owner, None)


class EventLogHandler(logging.Handler):
    def __init__(self, event_log: EventLog):
        super().__init__()
        self._event_log = event_log

    def emit(self, record: logging.LogRecord) -> None:
        self._event_log.add_event(self.format(record), getattr(record, "owners", ()))
