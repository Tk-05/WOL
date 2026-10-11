from __future__ import annotations

import hashlib

import pytest
import yaml

from wol_daemon.auth import AuthStore
from wol_daemon.web import load_instance_id


def test_legacy_key_becomes_read_only(tmp_path):
    path = tmp_path / "auth.yaml"
    legacy = "old-key"
    path.write_text(yaml.safe_dump({
        "api_key_hash": hashlib.sha256(legacy.encode()).hexdigest(),
        "api_key_created": "2026-01-01T00:00:00+01:00",
    }))

    store = AuthStore(path)

    key = store.check_api_key(legacy)
    assert key is not None
    assert key.scope == "read"
    assert key.created == "2026-01-01T00:00:00+01:00"
    saved = yaml.safe_load(path.read_text())
    assert "api_key_hash" not in saved
    assert len(saved["api_keys"]) == 1
    # Reloading doesn't migrate twice.
    assert len(AuthStore(path).api_keys) == 1


def test_keys_are_hashed_and_separately_revocable(tmp_path):
    store = AuthStore(tmp_path / "auth.yaml")
    ha = store.create_api_key("Home Assistant", "control")
    kuma = store.create_api_key("Uptime Kuma", "read")

    assert ha not in (tmp_path / "auth.yaml").read_text()
    assert store.check_api_key(ha).can_control
    assert not store.check_api_key(kuma).can_control

    revoked = store.revoke_api_key(store.check_api_key(ha).id)
    assert revoked.name == "Home Assistant"
    assert store.check_api_key(ha) is None
    assert store.check_api_key(kuma) is not None
    assert store.check_api_key("") is None


@pytest.mark.parametrize(
    ("name", "scope", "message"),
    [
        ("", "read", "name"),
        ("x" * 41, "read", "at most"),
        ("Kuma", "admin", "access level"),
        ("home assistant", "read", "already exists"),
    ],
)
def test_create_api_key_rejects(tmp_path, name, scope, message):
    store = AuthStore(tmp_path / "auth.yaml")
    store.create_api_key("Home Assistant", "read")
    with pytest.raises(ValueError, match=message):
        store.create_api_key(name, scope)


def test_instance_id_is_stable(tmp_path):
    first = load_instance_id(tmp_path / "instance_id")
    assert len(first) == 32
    assert load_instance_id(tmp_path / "instance_id") == first
