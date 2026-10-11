from __future__ import annotations

from dataclasses import dataclass, field

import pytest
from apscheduler.schedulers.background import BackgroundScheduler

from wol_daemon import web
from wol_daemon.config import (
    AppConfig,
    Cluster,
    Machine,
    NotificationConfig,
    SshShutdown,
    StatusCheckConfig,
    WolConfig,
)
from wol_daemon.eventlog import EventLog
from wol_daemon.events import EventBus
from wol_daemon.scheduler import owner_event_key

PASSWORD = "correct horse"
INSTANCE_ID = "0123456789abcdef0123456789abcdef"


@dataclass
class Calls:
    """What the web app asked the daemon to do, instead of sending packets or SSH commands."""

    machine: list[tuple[str, str, str]] = field(default_factory=list)  # (action, machine key, source)
    cluster: list[tuple[str, str, str]] = field(default_factory=list)  # (action, cluster key, source)
    result: str = "ok"  # what the next machine action records


@pytest.fixture
def calls() -> Calls:
    return Calls()


@pytest.fixture
def online(monkeypatch) -> set[str]:
    """IP addresses that answer pings; everything else counts as offline."""
    up: set[str] = set()
    monkeypatch.setattr(web, "is_host_up", lambda ip, timeout=5: ip in up)
    return up


@pytest.fixture
def events() -> EventBus:
    return EventBus()


@pytest.fixture
def app(tmp_path, monkeypatch, calls, online, events):
    for name in ("WOL_PROXMOX_HOST", "WOL_TARGET_MAC", "WOL_TARGET_IP"):
        monkeypatch.delenv(name, raising=False)
    config = AppConfig(
        machines=[
            Machine(
                key="nas", name="NAS", mac_address="AA:BB:CC:DD:EE:01", ip_address="10.0.0.1",
                shutdown=SshShutdown(host="10.0.0.1"), schedule=[],
            ),
            Machine(
                key="tv", name="TV", mac_address="AA:BB:CC:DD:EE:02", ip_address="10.0.0.2",
                shutdown=None, schedule=[],
            ),
        ],
        status_check=StatusCheckConfig(),
        wol=WolConfig(),
        notifications=NotificationConfig(),
        clusters=[Cluster(key="homelab", name="Homelab", members=["nas", "tv"], delay_seconds=0, schedule=[])],
    )
    event_log = EventLog(events=events)

    def machine_action(action: str):
        def run(machine: Machine, source: str) -> None:
            calls.machine.append((action, machine.key, source))
            detail = "boom" if calls.result == "error" else ""
            event_log.record_action(machine.key, action, source, calls.result, detail)
        return run

    def run_cluster(cluster: Cluster, action: str, source: str) -> None:
        calls.cluster.append((action, cluster.key, source))
        event_log.record_action(owner_event_key(cluster), action, source, "started", "")

    app = web.create_app(
        tmp_path / "config.yaml",
        config,
        BackgroundScheduler(),
        event_log,
        wake=machine_action("on"),
        shut_down=machine_action("off"),
        reboot=machine_action("reboot"),
        run_cluster=run_cluster,
        rebuild_jobs=lambda: None,
        events=events,
        instance_id=INSTANCE_ID,
    )
    app.config.update(TESTING=True)
    return app


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def logged_in_client(client):
    client.post("/setup", data={"password": PASSWORD, "confirm": PASSWORD})
    return client


@pytest.fixture
def make_key(logged_in_client):
    """Creates an API key through the settings page, like a user would, and returns it."""

    def make(name: str, scope: str) -> str:
        response = logged_in_client.post("/settings/api-key", data={"name": name, "scope": scope})
        assert response.status_code == 200
        html = response.get_data(as_text=True)
        start = html.index('<code class="api-key">') + len('<code class="api-key">')
        return html[start:html.index("</code>", start)]

    return make


def bearer(key: str) -> dict:
    return {"Authorization": f"Bearer {key}"}
