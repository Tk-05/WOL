from __future__ import annotations

import pytest

from .conftest import bearer


def test_api_needs_a_key(client):
    response = client.get("/api/machines")
    assert response.status_code == 401
    assert "error" in response.get_json()


def test_unknown_key_is_rejected(client, make_key):
    make_key("Home Assistant", "control")
    client.post("/logout")
    assert client.get("/api/machines", headers=bearer("wrong")).status_code == 401


def test_read_key_can_read_but_not_control(client, make_key, calls):
    key = make_key("Uptime Kuma", "read")
    client.post("/logout")

    assert client.get("/api/machines", headers=bearer(key)).status_code == 200
    response = client.post("/api/machines/nas/action/on", headers=bearer(key))
    assert response.status_code == 403
    assert "read-only" in response.get_json()["error"]
    assert client.post("/api/clusters/homelab/action/on", headers=bearer(key)).status_code == 403
    assert calls.machine == [] and calls.cluster == []


def test_revoked_key_stops_working(client, logged_in_client, make_key):
    key = make_key("Home Assistant", "control")
    settings = logged_in_client.get("/config").get_data(as_text=True)
    key_id = settings.split("/settings/api-key/")[1].split("/delete")[0]
    logged_in_client.post(f"/settings/api-key/{key_id}/delete")
    client.post("/logout")
    assert client.get("/api/machines", headers=bearer(key)).status_code == 401


def test_info(client, make_key):
    key = make_key("Home Assistant", "control")
    client.post("/logout")
    info = client.get("/api/info", headers=bearer(key)).get_json()
    assert len(info["instance_id"]) == 32
    assert info["status_timeout_seconds"] == 5
    assert info["access"] == "control"
    assert info["key_name"] == "Home Assistant"


def test_info_for_logged_in_browser(logged_in_client):
    info = logged_in_client.get("/api/info").get_json()
    assert info["access"] == "control"
    assert info["key_name"] is None


@pytest.fixture
def control_key(client, make_key):
    key = make_key("Home Assistant", "control")
    client.post("/logout")
    return bearer(key)


@pytest.mark.parametrize("action", ["on", "off", "reboot"])
def test_machine_action(client, control_key, calls, action):
    response = client.post(f"/api/machines/nas/action/{action}", headers=control_key)
    assert response.status_code == 200
    body = response.get_json()
    assert body["action"] == action
    assert body["result"] == "ok"
    assert body["source"] == "api:Home Assistant"
    assert calls.machine == [(action, "nas", "api:Home Assistant")]


def test_machine_action_shows_up_in_status(client, control_key):
    client.post("/api/machines/nas/action/on", headers=control_key)
    machine = client.get("/api/machines/nas", headers=control_key).get_json()
    assert machine["last_action"]["source"] == "api:Home Assistant"


def test_failed_machine_action_is_502(client, control_key, calls):
    calls.result = "error"
    response = client.post("/api/machines/nas/action/off", headers=control_key)
    assert response.status_code == 502
    assert response.get_json()["detail"] == "boom"


@pytest.mark.parametrize(
    ("path", "status"),
    [
        ("/api/machines/nope/action/on", 404),
        ("/api/machines/nas/action/explode", 400),
        ("/api/machines/tv/action/off", 409),  # wake-only machine
        ("/api/machines/tv/action/reboot", 409),
        ("/api/clusters/nope/action/on", 404),
        ("/api/clusters/homelab/action/reboot", 400),
    ],
)
def test_action_errors(client, control_key, calls, path, status):
    response = client.post(path, headers=control_key)
    assert response.status_code == status
    assert "error" in response.get_json()
    assert calls.machine == [] and calls.cluster == []


def test_wake_only_machine_can_be_woken(client, control_key, calls):
    assert client.post("/api/machines/tv/action/on", headers=control_key).status_code == 200
    assert calls.machine == [("on", "tv", "api:Home Assistant")]


def test_cluster_action_runs_in_background(client, control_key, calls):
    response = client.post("/api/clusters/homelab/action/off", headers=control_key)
    assert response.status_code == 202
    assert response.get_json()["result"] == "started"
    assert calls.cluster == [("off", "homelab", "api:Home Assistant")]


def test_logged_in_browser_actions_count_as_manual(logged_in_client, calls):
    assert logged_in_client.post("/api/machines/nas/action/on").status_code == 200
    assert calls.machine == [("on", "nas", "manual")]


def test_cross_site_post_is_refused(logged_in_client, calls):
    response = logged_in_client.post("/api/machines/nas/action/on", headers={"Origin": "http://evil.example"})
    assert response.status_code == 403
    assert calls.machine == []


def test_web_ui_actions_still_work(logged_in_client, calls):
    logged_in_client.post("/machines/nas/action/reboot")
    logged_in_client.post("/clusters/homelab/action/on")
    assert calls.machine == [("reboot", "nas", "manual")]
    assert calls.cluster == [("on", "homelab", "manual")]


def test_status_reflects_ping(client, control_key, online):
    online.add("10.0.0.1")
    cluster = client.get("/api/clusters/homelab", headers=control_key).get_json()
    assert cluster["state"] == "partial"
    assert [m["online"] for m in cluster["members"]] == [True, False]
