from __future__ import annotations

import json

import pytest

from wol_daemon import web
from wol_daemon.events import MAX_SUBSCRIBERS, EventBus

from .conftest import INSTANCE_ID, PASSWORD, bearer


class Stream:
    """Reads an open /api/events response one server-sent event at a time."""

    def __init__(self, response):
        assert response.status_code == 200
        assert response.mimetype == "text/event-stream"
        self.response = response
        self._chunks = iter(response.response)

    def next(self) -> tuple[str, dict | None]:
        chunk = next(self._chunks)
        chunk = chunk.decode() if isinstance(chunk, bytes) else chunk
        if chunk.startswith(":"):
            return "comment", None
        lines = dict(line.split(": ", 1) for line in chunk.strip().splitlines())
        return lines["event"], json.loads(lines["data"])

    def close(self) -> None:
        self.response.close()


@pytest.fixture
def open_stream(client, make_key):
    key = make_key("Home Assistant", "read")  # reading the stream needs no Control access
    client.post("/logout")
    streams: list[Stream] = []

    def open_() -> Stream:
        stream = Stream(client.get("/api/events", headers=bearer(key), buffered=False))
        streams.append(stream)
        return stream

    yield open_
    for stream in streams:
        stream.close()


def test_stream_needs_a_key(client):
    assert client.get("/api/events").status_code == 401


def test_stream_starts_with_hello(open_stream):
    assert open_stream().next() == ("hello", {"type": "hello", "instance_id": INSTANCE_ID})


def test_actions_and_state_changes_are_pushed(app, open_stream, events):
    stream = open_stream()
    stream.next()
    events.action_recorded("nas", "on", "ok")
    events.action_recorded("cluster@homelab", "off", "started")
    events.state_changed("nas", True)
    assert stream.next() == ("action", {"type": "action", "kind": "machine", "key": "nas", "action": "on", "result": "ok"})
    assert stream.next() == (
        "action", {"type": "action", "kind": "cluster", "key": "homelab", "action": "off", "result": "started"}
    )
    assert stream.next() == ("state", {"type": "state", "kind": "machine", "key": "nas", "online": True})


@pytest.fixture
def browser(app):
    """A second, logged-in browser next to the API client holding the stream."""
    other = app.test_client()
    other.post("/login", data={"password": PASSWORD})
    return other


def test_ui_action_is_pushed(open_stream, browser):
    stream = open_stream()
    stream.next()
    browser.post("/machines/nas/action/reboot")
    event, data = stream.next()
    assert event == "action"
    assert (data["key"], data["action"]) == ("nas", "reboot")


def test_config_change_is_pushed(open_stream, browser):
    stream = open_stream()
    stream.next()
    browser.post(
        "/machines/nas/schedule/add", data={"name": "Night", "days": ["mon"], "time": "23:00", "action": "off"}
    )
    assert stream.next() == ("config", {"type": "config"})


def test_keepalive_while_idle(monkeypatch, open_stream):
    monkeypatch.setattr(web, "KEEPALIVE_SECONDS", 0.01)
    stream = open_stream()
    stream.next()
    assert stream.next() == ("comment", None)


def test_closing_the_stream_unsubscribes(open_stream, events):
    stream = open_stream()
    stream.next()
    assert len(events._subscribers) == 1
    stream.close()
    assert len(events._subscribers) == 0


def test_stream_limit(client, make_key, events):
    key = make_key("Home Assistant", "read")
    client.post("/logout")
    held = [events.subscribe() for _ in range(MAX_SUBSCRIBERS)]
    assert all(held)
    response = client.get("/api/events", headers=bearer(key))
    assert response.status_code == 503


def test_slow_subscriber_doesnt_block_others():
    bus = EventBus()
    slow, fast = bus.subscribe(), bus.subscribe()
    for _ in range(500):
        bus.config_changed()
    assert slow.qsize() == fast.qsize() == 100
