from __future__ import annotations

import logging
import signal
import sys
from pathlib import Path

import requests
from apscheduler.schedulers.background import BackgroundScheduler

from .config import AppConfig, Machine, ProxmoxShutdown, ScheduleRule, SshShutdown, load_config, save_config
from .eventlog import EventLog, EventLogHandler
from .magicpacket import send_magic_packet
from .notify import send_notification
from .proxmox import ProxmoxClient
from .scheduler import build_scheduler
from .ssh_shutdown import SshClient, SshShutdownError
from .status import is_host_up
from .watchdog import schedule_wake_check
from .web import create_app

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("wol_daemon")


def turn_on(config: AppConfig, machine: Machine, event_log: EventLog, scheduler: BackgroundScheduler, source: str = "schedule") -> None:
    if is_host_up(machine.ip_address, config.status_check.timeout_seconds):
        logger.info("'%s' is already reachable, no magic packet needed", machine.name)
        event_log.record_action(machine.key, "on", source, "skipped", "already online")
        return
    logger.info("Sending magic packet to '%s' (%s)", machine.name, machine.mac_address)
    send_magic_packet(machine.mac_address)
    event_log.record_action(machine.key, "on", source, "ok")
    schedule_wake_check(scheduler, config, machine, event_log)


def turn_off(config: AppConfig, machine: Machine, event_log: EventLog, source: str = "schedule") -> None:
    if not is_host_up(machine.ip_address, config.status_check.timeout_seconds):
        logger.info("'%s' is already offline, no shutdown needed", machine.name)
        event_log.record_action(machine.key, "off", source, "skipped", "already offline")
        return

    if machine.shutdown is None:
        logger.warning("'%s' has no shutdown method configured, cannot power it off", machine.name)
        event_log.record_action(machine.key, "off", source, "error", "no shutdown method configured")
        return

    logger.info("Shutting down '%s'", machine.name)
    try:
        _shutdown_client(machine.shutdown).shutdown_node()
    except (requests.RequestException, SshShutdownError) as exc:
        logger.exception("Shutdown failed for '%s'", machine.name)
        event_log.record_action(machine.key, "off", source, "error", str(exc))
        send_notification(config.notifications, f"Shutdown failed for '{machine.name}' ({machine.ip_address}): {exc}")
        return
    event_log.record_action(machine.key, "off", source, "ok")


def _shutdown_client(shutdown: ProxmoxShutdown | SshShutdown):
    if isinstance(shutdown, ProxmoxShutdown):
        return ProxmoxClient(shutdown)
    return SshClient(shutdown)


def on_rule_skipped(machine: Machine, rule: ScheduleRule, config: AppConfig, config_path: Path, event_log: EventLog) -> None:
    logger.info("Skipping scheduled '%s' for '%s' rule '%s' (skip requested)", rule.action, machine.name, rule.name)
    event_log.record_action(machine.key, rule.action, "schedule", "skipped", f"skipped by user ({rule.name})")
    save_config(config, config_path)


def main() -> None:
    config_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent / "config.yaml"
    config = load_config(config_path)
    event_log = EventLog()

    event_handler = EventLogHandler(event_log)
    event_handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(event_handler)

    scheduler = build_scheduler(
        config.machines,
        on_action=lambda m: turn_on(config, m, event_log, scheduler, "schedule"),
        off_action=lambda m: turn_off(config, m, event_log, "schedule"),
        on_skip=lambda m, rule: on_rule_skipped(m, rule, config, config_path, event_log),
    )
    scheduler.start()
    logger.info("WOL daemon started, %d machine(s) loaded", len(config.machines))
    if not config.machines:
        logger.warning("No machines configured yet (%s is missing or empty) - add one in the web UI", config_path)

    app = create_app(config_path, config, scheduler, event_log, turn_on, turn_off, on_rule_skipped)

    def handle_shutdown(signum, frame):
        logger.info("Shutting down WOL daemon...")
        scheduler.shutdown()
        sys.exit(0)

    signal.signal(signal.SIGTERM, handle_shutdown)
    signal.signal(signal.SIGINT, handle_shutdown)

    app.run(host="0.0.0.0", port=9090)


if __name__ == "__main__":
    main()
