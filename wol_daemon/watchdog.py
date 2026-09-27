from __future__ import annotations

import logging
from datetime import datetime, timedelta

from apscheduler.schedulers.background import BackgroundScheduler

from .config import AppConfig, Machine
from .eventlog import EventLog
from .magicpacket import send_magic_packet
from .notify import send_notification
from .status import is_host_up

logger = logging.getLogger("wol_daemon")


def schedule_wake_check(scheduler: BackgroundScheduler, config: AppConfig, machine: Machine, event_log: EventLog) -> None:
    scheduler.add_job(
        lambda: _verify_wake(scheduler, config, machine, event_log, config.wol.max_retries),
        trigger="date",
        run_date=datetime.now() + timedelta(seconds=config.wol.verify_after_seconds),
    )


def _verify_wake(
    scheduler: BackgroundScheduler,
    config: AppConfig,
    machine: Machine,
    event_log: EventLog,
    remaining_retries: int,
) -> None:
    if is_host_up(machine.ip_address, config.status_check.timeout_seconds):
        logger.info("'%s' reachable after wake attempt", machine.name)
        return

    if remaining_retries <= 0:
        message = f"WOL failed: '{machine.name}' ({machine.ip_address}) unreachable after multiple attempts"
        logger.error(message)
        event_log.record_action(machine.key, "on", "watchdog", "error", "unreachable after retries")
        send_notification(config.notifications, message)
        return

    logger.warning(
        "'%s' still not reachable, resending magic packet (%d attempt(s) left)", machine.name, remaining_retries
    )
    send_magic_packet(machine.mac_address)
    scheduler.add_job(
        lambda: _verify_wake(scheduler, config, machine, event_log, remaining_retries - 1),
        trigger="date",
        run_date=datetime.now() + timedelta(seconds=config.wol.retry_interval_seconds),
    )
