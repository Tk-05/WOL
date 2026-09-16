from __future__ import annotations

import logging
from datetime import datetime, timedelta

from apscheduler.schedulers.background import BackgroundScheduler

from .config import AppConfig
from .eventlog import EventLog
from .magicpacket import send_magic_packet
from .notify import send_notification
from .status import is_host_up

logger = logging.getLogger("wol_daemon")


def schedule_wake_check(scheduler: BackgroundScheduler, config: AppConfig, event_log: EventLog) -> None:
    scheduler.add_job(
        lambda: _verify_wake(scheduler, config, event_log, config.wol.max_retries),
        trigger="date",
        run_date=datetime.now() + timedelta(seconds=config.wol.verify_after_seconds),
    )


def _verify_wake(scheduler: BackgroundScheduler, config: AppConfig, event_log: EventLog, remaining_retries: int) -> None:
    if is_host_up(config.target.ip_address, config.status_check.timeout_seconds):
        logger.info("Node reachable after wake attempt")
        return

    if remaining_retries <= 0:
        message = f"WOL failed: {config.target.ip_address} unreachable after multiple attempts"
        logger.error(message)
        event_log.record_action("on", "watchdog", "error", "unreachable after retries")
        send_notification(config.notifications, message)
        return

    logger.warning("Node still not reachable, resending magic packet (%d attempt(s) left)", remaining_retries)
    send_magic_packet(config.target.mac_address)
    scheduler.add_job(
        lambda: _verify_wake(scheduler, config, event_log, remaining_retries - 1),
        trigger="date",
        run_date=datetime.now() + timedelta(seconds=config.wol.retry_interval_seconds),
    )
