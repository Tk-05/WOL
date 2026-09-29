from __future__ import annotations

import logging

import requests

from .config import ProxmoxShutdown

logger = logging.getLogger(__name__)


class ProxmoxClient:
    def __init__(self, config: ProxmoxShutdown):
        self._config = config
        self._auth_header = f"PVEAPIToken={config.token_id}={config.token_secret}"

    def shutdown_node(self) -> None:
        self._send_command("shutdown")
        logger.info("Shutdown command sent to Proxmox node '%s'", self._config.node)

    def reboot_node(self) -> None:
        self._send_command("reboot")
        logger.info("Reboot command sent to Proxmox node '%s'", self._config.node)

    def _send_command(self, command: str) -> None:
        url = f"{self._config.host}/api2/json/nodes/{self._config.node}/status"
        response = requests.post(
            url,
            headers={"Authorization": self._auth_header},
            data={"command": command},
            verify=self._config.verify_ssl,
            timeout=10,
        )
        response.raise_for_status()
