from __future__ import annotations

import logging
import subprocess

from .config import SshShutdown

logger = logging.getLogger(__name__)


class SshShutdownError(Exception):
    pass


class SshClient:
    def __init__(self, config: SshShutdown):
        self._config = config

    def shutdown_node(self) -> None:
        cmd = [
            "ssh",
            "-o", "StrictHostKeyChecking=accept-new",
            "-o", "BatchMode=yes",
            "-o", "ConnectTimeout=10",
            "-p", str(self._config.port),
        ]
        if self._config.private_key_path:
            cmd += ["-i", self._config.private_key_path]
        cmd += [f"{self._config.username}@{self._config.host}", self._config.command]

        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        except subprocess.TimeoutExpired as exc:
            raise SshShutdownError(f"SSH command timed out: {exc}") from exc
        except OSError as exc:
            raise SshShutdownError(f"Could not run ssh: {exc}") from exc

        if result.returncode != 0:
            raise SshShutdownError(f"ssh exited with {result.returncode}: {result.stderr.strip()}")
        logger.info("Shutdown command sent to '%s' via SSH", self._config.host)
