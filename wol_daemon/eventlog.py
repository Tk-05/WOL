from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from threading import Lock


@dataclass
class ActionRecord:
    timestamp: datetime
    action: str  # "on" | "off"
    source: str  # "schedule" | "manuell"
    result: str  # "ok" | "skipped" | "error"
    detail: str = ""


class EventLog:
    def __init__(self, max_events: int = 200):
        self._lock = Lock()
        self._events: deque[str] = deque(maxlen=max_events)
        self.last_action: ActionRecord | None = None

    def add_event(self, message: str) -> None:
        with self._lock:
            self._events.append(f"{datetime.now():%Y-%m-%d %H:%M:%S}  {message}")

    def record_action(self, action: str, source: str, result: str, detail: str = "") -> None:
        with self._lock:
            self.last_action = ActionRecord(datetime.now(), action, source, result, detail)

    def recent_events(self, limit: int = 20) -> list[str]:
        with self._lock:
            return list(self._events)[-limit:][::-1]


class EventLogHandler(logging.Handler):
    def __init__(self, event_log: EventLog):
        super().__init__()
        self._event_log = event_log

    def emit(self, record: logging.LogRecord) -> None:
        self._event_log.add_event(self.format(record))
