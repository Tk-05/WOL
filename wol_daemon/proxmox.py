from __future__ import annotations

import logging

import requests

from .config import ProxmoxConfig

logger = logging.getLogger(__name__)


class ProxmoxClient:
    def __init__(self, config: ProxmoxConfig):
        self._config = config
        self._auth_header = f"PVEAPIToken={config.token_id}={config.token_secret}"

    def shutdown_node(self) -> None:
        url = f"{self._config.host}/api2/json/nodes/{self._config.node}/status"
        response = requests.post(
            url,
            headers={"Authorization": self._auth_header},
            data={"command": "shutdown"},
            verify=self._config.verify_ssl,
            timeout=10,
        )
        response.raise_for_status()
        logger.info("Shutdown command sent to Proxmox node '%s'", self._config.node)
