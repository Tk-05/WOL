from __future__ import annotations

import json
import logging
import os
import sys
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from threading import Lock

# Rewrite the file down to what's actually kept after this many appended records.
COMPACT_EVERY = 1000

# State transitions kept per machine, for the uptime bar. Only actual changes are stored (see
# record_state), so this covers many weeks even for a machine that flips several times a day.
STATE_HISTORY_LIMIT = 300


@dataclass
class ActionRecord:
    timestamp: datetime
    machine_key: str  # or "cluster@<key>" for a cluster
    action: str  # "on" | "off"
    source: str  # "schedule" | "manual" | "watchdog" | "cluster:<key>"
    result: str  # "ok" | "skipped" | "error"; clusters also "started" | "cancelled"
    detail: str = ""


@dataclass
class StateSample:
    """One observed change of a machine's reachability, oldest first in state_history_for."""

    timestamp: datetime
    up: bool


@dataclass
class _Event:
    seq: int
    line: str
    owners: tuple[str, ...]


class EventLog:
    """One global list for the overview, plus one per machine/cluster, and the last action of
    each. Log records are routed to owners via logging's extra={"owners": [...]}.

    Also keeps, per machine, a short history of reachability changes (see record_state), used
    for the uptime bar on its page.

    With a path, everything is also appended to a JSON-lines file and read back on start, so
    it survives restarts. The file is periodically rewritten to just what's kept in memory."""

    def __init__(self, max_events: int = 200, max_per_owner: int = 50, path: Path | None = None):
        self._lock = Lock()
        self._events: deque[_Event] = deque(maxlen=max_events)
        self._owner_events: dict[str, deque[_Event]] = {}
        self._max_per_owner = max_per_owner
        self._last_actions: dict[str, ActionRecord] = {}
        self._state_history: dict[str, deque[StateSample]] = {}
        self._last_known_state: dict[str, bool] = {}
        self._seq = 0
        self._path = Path(path) if path is not None else None
        self._appended = 0
        if self._path is not None:
            self._load()
            self._compact()

    def add_event(self, message: str, owners: list[str] | tuple[str, ...] = ()) -> None:
        line = f"{datetime.now():%Y-%m-%d %H:%M:%S}  {message}"
        with self._lock:
            self._seq += 1
            event = _Event(self._seq, line, tuple(owners))
            self._remember(event)
            self._append(_event_to_dict(event))

    def record_action(self, machine_key: str, action: str, source: str, result: str, detail: str = "") -> None:
        record = ActionRecord(datetime.now(), machine_key, action, source, result, detail)
        with self._lock:
            self._last_actions[machine_key] = record
            self._append(_action_to_dict(record))

    def last_action_for(self, machine_key: str) -> ActionRecord | None:
        with self._lock:
            return self._last_actions.get(machine_key)

    def record_state(self, machine_key: str, up: bool) -> None:
        """Called whenever a machine's reachability is freshly checked (the periodic status
        poll, but also a confirmed wake or an already-on/off skip), so the uptime bar reflects
        reality even when a machine is switched by hand. A no-op unless the state actually
        changed, so polling often stays cheap."""
        with self._lock:
            if self._last_known_state.get(machine_key) == up:
                return
            self._last_known_state[machine_key] = up
            sample = StateSample(datetime.now(), up)
            self._state_history.setdefault(machine_key, deque(maxlen=STATE_HISTORY_LIMIT)).append(sample)
            self._append(_state_to_dict(machine_key, sample))

    def state_history_for(self, machine_key: str) -> list[StateSample]:
        """Oldest first. Only the transitions themselves - assume the state holds between two
        entries, and from the last one up to now."""
        with self._lock:
            return list(self._state_history.get(machine_key, ()))

    def recent_events(self, limit: int = 20) -> list[str]:
        with self._lock:
            return [e.line for e in list(self._events)[-limit:][::-1]]

    def recent_events_for(self, owner: str, limit: int = 30) -> list[str]:
        with self._lock:
            return [e.line for e in list(self._owner_events.get(owner, ()))[-limit:][::-1]]

    def forget(self, owner: str) -> None:
        """Drop a deleted machine's/cluster's history, so a new one reusing the key starts
        clean. Written to the file too, or the history would come back on the next start."""
        with self._lock:
            self._forget(owner)
            self._append({"type": "forget", "owner": owner})

    # --- internals, called with the lock held (or from __init__) ---

    def _remember(self, event: _Event) -> None:
        self._events.append(event)
        for owner in event.owners:
            self._owner_events.setdefault(owner, deque(maxlen=self._max_per_owner)).append(event)

    def _forget(self, owner: str) -> None:
        self._owner_events.pop(owner, None)
        self._last_actions.pop(owner, None)
        self._state_history.pop(owner, None)
        self._last_known_state.pop(owner, None)
        # Events shared with the overview or other owners stay, but must no longer point at
        # this owner, or a rewrite of the file would hand them to a new owner with this key.
        for event in self._all_events():
            if owner in event.owners:
                event.owners = tuple(o for o in event.owners if o != owner)

    def _all_events(self) -> list[_Event]:
        unique = {id(e): e for e in self._events}
        for events in self._owner_events.values():
            unique.update((id(e), e) for e in events)
        return sorted(unique.values(), key=lambda e: e.seq)

    def _append(self, record: dict) -> None:
        if self._path is None:
            return
        try:
            with self._path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError as exc:
            self._give_up(exc)
            return
        self._appended += 1
        if self._appended >= COMPACT_EVERY:
            self._compact()

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            lines = self._path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            self._give_up(exc)
            return
        for raw in lines:
            try:
                record = json.loads(raw)
                kind = record.get("type")
                if kind == "event":
                    event = _Event(int(record["seq"]), str(record["line"]), tuple(record.get("owners", ())))
                    self._seq = max(self._seq, event.seq)
                    self._remember(event)
                elif kind == "action":
                    action = _action_from_dict(record)
                    self._last_actions[action.machine_key] = action
                elif kind == "state":
                    key = str(record["key"])
                    sample = StateSample(datetime.fromisoformat(record["time"]), bool(record["up"]))
                    self._state_history.setdefault(key, deque(maxlen=STATE_HISTORY_LIMIT)).append(sample)
                    self._last_known_state[key] = sample.up
                elif kind == "forget":
                    self._forget(record["owner"])
            except (ValueError, KeyError, TypeError, AttributeError):
                continue  # e.g. a line cut off by a crash mid-write

    def _compact(self) -> None:
        if self._path is None:
            return
        records = [_event_to_dict(e) for e in self._all_events()]
        records += [_action_to_dict(a) for a in self._last_actions.values()]
        for key, samples in self._state_history.items():
            records += [_state_to_dict(key, s) for s in samples]
        tmp = self._path.with_name(self._path.name + ".tmp")
        try:
            tmp.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records), encoding="utf-8")
            os.replace(tmp, self._path)
        except OSError as exc:
            self._give_up(exc)
            return
        self._appended = 0

    def _give_up(self, exc: OSError) -> None:
        # Not via logging: this runs inside the logging handler, and would recurse.
        print(f"Event log can't be saved to {self._path}, keeping it in memory only: {exc}", file=sys.stderr)
        self._path = None


def _event_to_dict(event: _Event) -> dict:
    return {"type": "event", "seq": event.seq, "line": event.line, "owners": list(event.owners)}


def _action_to_dict(record: ActionRecord) -> dict:
    return {
        "type": "action",
        "time": record.timestamp.isoformat(timespec="seconds"),
        "key": record.machine_key,
        "action": record.action,
        "source": record.source,
        "result": record.result,
        "detail": record.detail,
    }


def _state_to_dict(machine_key: str, sample: StateSample) -> dict:
    return {"type": "state", "key": machine_key, "time": sample.timestamp.isoformat(timespec="seconds"), "up": sample.up}


def _action_from_dict(data: dict) -> ActionRecord:
    return ActionRecord(
        timestamp=datetime.fromisoformat(data["time"]),
        machine_key=data["key"],
        action=data["action"],
        source=data["source"],
        result=data["result"],
        detail=data.get("detail", ""),
    )


class EventLogHandler(logging.Handler):
    def __init__(self, event_log: EventLog):
        super().__init__()
        self._event_log = event_log

    def emit(self, record: logging.LogRecord) -> None:
        self._event_log.add_event(self.format(record), getattr(record, "owners", ()))
