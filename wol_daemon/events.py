from __future__ import annotations

import json
import queue
from threading import Lock

# Clients like Home Assistant hold a GET /api/events stream open; each one costs a thread
# of the web server, so there's a cap.
MAX_SUBSCRIBERS = 10
# Sent while nothing happens, so both sides notice a dead connection (and the server frees
# the thread) within this many seconds.
KEEPALIVE_SECONDS = 15
_QUEUE_SIZE = 100


class EventBus:
    """Fans out change notifications (an action was recorded, a machine came up or went
    down, the configuration was saved) to the open /api/events streams. Events only say
    what changed; clients fetch the details from the status API. A client that falls more
    than _QUEUE_SIZE events behind misses some, which is fine since clients also poll."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._subscribers: set[queue.Queue] = set()

    def subscribe(self) -> queue.Queue | None:
        """None if MAX_SUBSCRIBERS streams are already open."""
        with self._lock:
            if len(self._subscribers) >= MAX_SUBSCRIBERS:
                return None
            subscriber: queue.Queue = queue.Queue(maxsize=_QUEUE_SIZE)
            self._subscribers.add(subscriber)
            return subscriber

    def unsubscribe(self, subscriber: queue.Queue) -> None:
        with self._lock:
            self._subscribers.discard(subscriber)

    def publish(self, event: dict) -> None:
        with self._lock:
            subscribers = list(self._subscribers)
        for subscriber in subscribers:
            try:
                subscriber.put_nowait(event)
            except queue.Full:
                pass

    def action_recorded(self, owner_key: str, action: str, result: str) -> None:
        """owner_key as in the event log: a machine key, or "cluster@<key>" for a cluster."""
        kind, _, key = owner_key.rpartition("@")
        self.publish({"type": "action", "kind": kind or "machine", "key": key, "action": action, "result": result})

    def state_changed(self, machine_key: str, online: bool) -> None:
        self.publish({"type": "state", "kind": "machine", "key": machine_key, "online": online})

    def config_changed(self) -> None:
        self.publish({"type": "config"})


def format_sse(event: dict) -> str:
    """One server-sent event: its type as the event name, the whole dict as JSON data."""
    return f"event: {event['type']}\ndata: {json.dumps(event)}\n\n"
