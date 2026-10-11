from __future__ import annotations

import logging
import socket

from zeroconf import IPVersion, ServiceInfo, Zeroconf

logger = logging.getLogger("wol_daemon")

# Home Assistant's integration listens for this type (its manifest.json, "zeroconf").
SERVICE_TYPE = "_wol-daemon._tcp.local."


def primary_ipv4() -> str | None:
    """Address of the interface the default route goes out of: the one other machines on
    the LAN reach this host by. With host networking there are more (docker0 and the like),
    which would be no use to Home Assistant. Connecting a UDP socket sends nothing."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        try:
            probe.connect(("192.0.2.1", 9))  # TEST-NET-1, never routed anywhere real
            address = probe.getsockname()[0]
        except OSError:
            return None
    return None if address.startswith("127.") else address


def service_info(instance_id: str, address: str, port: int) -> ServiceInfo:
    hostname = socket.gethostname().split(".")[0] or "wol-daemon"
    return ServiceInfo(
        SERVICE_TYPE,
        f"WOL Daemon ({hostname}).{SERVICE_TYPE}",
        addresses=[socket.inet_aton(address)],
        port=port,
        # The instance ID lets Home Assistant tell whether it already knows this daemon
        # (and just update its address) before asking for an API key.
        properties={"id": instance_id},
        server=f"{hostname}.local.",
    )


class Discovery:
    """Announces the daemon on the LAN via mDNS/zeroconf, so Home Assistant offers to set
    it up on its own. Failing to announce (e.g. no multicast) only logs a warning."""

    def __init__(self, instance_id: str, port: int) -> None:
        self._zeroconf: Zeroconf | None = None
        self._info: ServiceInfo | None = None
        address = primary_ipv4()
        if address is None:
            logger.warning("Network discovery off: couldn't determine this host's LAN address")
            return
        try:
            self._info = service_info(instance_id, address, port)
            self._zeroconf = Zeroconf(ip_version=IPVersion.V4Only)
            self._zeroconf.register_service(self._info, allow_name_change=True)
        except Exception:
            logger.warning("Network discovery off: announcing via zeroconf failed", exc_info=True)
            self.close()
            return
        logger.info("Announcing this daemon on the network as %s:%d", address, port)

    def close(self) -> None:
        if self._zeroconf is None:
            return
        try:
            if self._info is not None:
                self._zeroconf.unregister_service(self._info)
        finally:
            self._zeroconf.close()
            self._zeroconf = None
