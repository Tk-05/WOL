from __future__ import annotations

import logging
import signal
import sys
from pathlib import Path

import requests
from apscheduler.schedulers.background import BackgroundScheduler

from .config import AppConfig, load_config
from .eventlog import EventLog, EventLogHandler
from .magicpacket import send_magic_packet
from .notify import send_notification
from .proxmox import ProxmoxClient
from .scheduler import build_scheduler
from .status import is_host_up
from .watchdog import schedule_wake_check
from .web import create_app

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("wol_daemon")


def turn_on(config: AppConfig, event_log: EventLog, scheduler: BackgroundScheduler, source: str = "schedule") -> None:
    if is_host_up(config.target.ip_address, config.status_check.timeout_seconds):
        logger.info("Node is already reachable, no magic packet needed")
        event_log.record_action("on", source, "skipped", "already online")
        return
    logger.info("Sending magic packet to %s", config.target.mac_address)
    send_magic_packet(config.target.mac_address)
    event_log.record_action("on", source, "ok")
    schedule_wake_check(scheduler, config, event_log)


def turn_off(config: AppConfig, proxmox_client: ProxmoxClient, event_log: EventLog, source: str = "schedule") -> None:
    if not is_host_up(config.target.ip_address, config.status_check.timeout_seconds):
        logger.info("Node is already offline, no shutdown needed")
        event_log.record_action("off", source, "skipped", "already offline")
        return
    logger.info("Shutting down node via Proxmox API")
    try:
        proxmox_client.shutdown_node()
    except requests.RequestException as exc:
        logger.exception("Shutdown failed")
        event_log.record_action("off", source, "error", str(exc))
        send_notification(config.notifications, f"Shutdown failed for {config.target.ip_address}: {exc}")
        return
    event_log.record_action("off", source, "ok")


def main() -> None:
    config_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent / "config.yaml"
    config = load_config(config_path)
    proxmox_client = ProxmoxClient(config.proxmox)
    event_log = EventLog()

    event_handler = EventLogHandler(event_log)
    event_handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(event_handler)

    scheduler = build_scheduler(
        config.schedule,
        on_action=lambda: turn_on(config, event_log, scheduler, "schedule"),
        off_action=lambda: turn_off(config, proxmox_client, event_log, "schedule"),
    )
    scheduler.start()
    logger.info("WOL daemon started, %d schedule rule(s) loaded", len(config.schedule))

    app = create_app(config_path, config, scheduler, proxmox_client, event_log, turn_on, turn_off)

    def handle_shutdown(signum, frame):
        logger.info("Shutting down WOL daemon...")
        scheduler.shutdown()
        sys.exit(0)

    signal.signal(signal.SIGTERM, handle_shutdown)
    signal.signal(signal.SIGINT, handle_shutdown)

    app.run(host="0.0.0.0", port=9090)


if __name__ == "__main__":
    main()
