from __future__ import annotations

import logging
from datetime import date

import pytest

from .conftest import PASSWORD, bearer


@pytest.fixture
def browser(app, logged_in_client):
    """A logged-in browser of its own (the password is set first via logged_in_client)."""
    other = app.test_client()
    other.post("/login", data={"password": PASSWORD})
    return other


@pytest.fixture
def control_key(client, make_key):
    key = make_key("Home Assistant", "control")
    client.post("/logout")
    return bearer(key)


def add_rule(browser, path: str, name: str, time: str) -> None:
    every_day = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
    response = browser.post(f"{path}/schedule/add", data={"name": name, "days": every_day, "time": time, "action": "on"})
    assert response.status_code == 302


@pytest.mark.parametrize("path", ["/machines/nas", "/clusters/homelab"])
def test_skip_and_unskip_next_run(client, browser, control_key, path):
    add_rule(browser, path, "Morning", "07:00")
    add_rule(browser, path, "Evening", "19:00")
    api = f"/api{path}"
    upcoming = client.get(api, headers=control_key).get_json()["next_action"]
    assert upcoming["will_be_skipped"] is False

    response = client.post(f"{api}/skip-next", headers=control_key)
    assert response.status_code == 200
    skipped = response.get_json()
    assert skipped["rule"] == upcoming["rule"]
    assert skipped["time"] == upcoming["time"]
    assert skipped["will_be_skipped"] is True
    # Skipping again changes nothing.
    assert client.post(f"{api}/skip-next", headers=control_key).get_json() == skipped

    response = client.delete(f"{api}/skip-next", headers=control_key)
    assert response.get_json()["will_be_skipped"] is False
    assert client.delete(f"{api}/skip-next", headers=control_key).status_code == 200


def test_only_the_next_rule_is_skipped(app, client, browser, control_key):
    add_rule(browser, "/machines/nas", "Morning", "07:00")
    add_rule(browser, "/machines/nas", "Evening", "19:00")
    skipped = client.post("/api/machines/nas/skip-next", headers=control_key).get_json()
    settings = browser.get("/machines/nas").get_data(as_text=True)
    assert settings.count("Undo skip") == 1
    assert skipped["time"][:10] >= date.today().isoformat()


def test_skip_is_logged_with_its_source(client, browser, control_key, caplog):
    add_rule(browser, "/machines/nas", "Morning", "07:00")
    with caplog.at_level(logging.INFO, logger="wol_daemon"):
        client.post("/api/machines/nas/skip-next", headers=control_key)
        client.delete("/api/machines/nas/skip-next", headers=control_key)
    messages = [r.getMessage() for r in caplog.records]
    assert any("'Morning' will be skipped on" in m and "via api:Home Assistant" in m for m in messages)
    assert any("skip of rule 'Morning' cancelled (via api:Home Assistant)" in m for m in messages)


def test_nothing_scheduled(client, control_key):
    response = client.post("/api/machines/nas/skip-next", headers=control_key)
    assert response.status_code == 409
    assert "no scheduled run" in response.get_json()["error"]


def test_unknown_owner(client, control_key):
    assert client.post("/api/machines/nope/skip-next", headers=control_key).status_code == 404
    assert client.delete("/api/clusters/nope/skip-next", headers=control_key).status_code == 404


def test_read_only_key_cant_skip(client, browser, make_key):
    add_rule(browser, "/machines/nas", "Morning", "07:00")
    key = make_key("Kuma", "read")
    client.post("/logout")
    assert client.post("/api/machines/nas/skip-next", headers=bearer(key)).status_code == 403
    assert client.delete("/api/machines/nas/skip-next", headers=bearer(key)).status_code == 403
