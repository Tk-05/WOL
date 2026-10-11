from __future__ import annotations

import socket

from wol_daemon import discovery
from wol_daemon.discovery import SERVICE_TYPE, Discovery, service_info


def test_service_info():
    info = service_info("abc123", "192.168.1.5", 9090)
    assert info.type == SERVICE_TYPE
    assert info.name.endswith("." + SERVICE_TYPE)
    assert info.port == 9090
    assert info.addresses == [socket.inet_aton("192.168.1.5")]
    assert info.properties == {b"id": b"abc123"}


class FakeZeroconf:
    instances: list[FakeZeroconf] = []

    def __init__(self, **kwargs):
        self.registered = []
        self.closed = False
        FakeZeroconf.instances.append(self)

    def register_service(self, info, allow_name_change=False):
        self.registered.append(info)

    def unregister_service(self, info):
        self.registered.remove(info)

    def close(self):
        self.closed = True


def test_announces_and_withdraws(monkeypatch):
    FakeZeroconf.instances.clear()
    monkeypatch.setattr(discovery, "Zeroconf", FakeZeroconf)
    monkeypatch.setattr(discovery, "primary_ipv4", lambda: "192.168.1.5")
    announced = Discovery("abc123", 9090)
    zc = FakeZeroconf.instances[0]
    assert [info.port for info in zc.registered] == [9090]
    announced.close()
    assert zc.registered == [] and zc.closed
    announced.close()  # twice is fine


def test_no_lan_address_means_no_announcement(monkeypatch):
    FakeZeroconf.instances.clear()
    monkeypatch.setattr(discovery, "Zeroconf", FakeZeroconf)
    monkeypatch.setattr(discovery, "primary_ipv4", lambda: None)
    Discovery("abc123", 9090).close()
    assert FakeZeroconf.instances == []


def test_zeroconf_failure_doesnt_stop_the_daemon(monkeypatch):
    def broken(**kwargs):
        raise OSError("no multicast")

    monkeypatch.setattr(discovery, "Zeroconf", broken)
    monkeypatch.setattr(discovery, "primary_ipv4", lambda: "192.168.1.5")
    Discovery("abc123", 9090).close()
