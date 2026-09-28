from __future__ import annotations

import functools
import logging
import signal
import sys
from datetime import datetime, timedelta
from pathlib import Path

import requests
from apscheduler.schedulers.background import BackgroundScheduler

from .config import (
    AppConfig,
    Cluster,
    Machine,
    ProxmoxShutdown,
    ScheduleRule,
    SshShutdown,
    load_config,
    save_config,
)
from .eventlog import EventLog, EventLogHandler
from .magicpacket import send_magic_packet
from .notify import send_notification
from .proxmox import ProxmoxClient
from .scheduler import build_scheduler, owner_event_key, update_jobs
from .ssh_shutdown import SshClient, SshShutdownError
from .status import is_host_up
from .watchdog import schedule_wake_check
from .web import create_app

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("wol_daemon")


def turn_on(
    config: AppConfig,
    machine: Machine,
    event_log: EventLog,
    scheduler: BackgroundScheduler,
    source: str = "schedule",
    log_owners: tuple[str, ...] = (),
) -> None:
    """log_owners: further event logs (e.g. the triggering cluster's) that get these lines too."""
    owners = (machine.key, *log_owners)
    extra = {"owners": owners}
    if is_host_up(machine.ip_address, config.status_check.timeout_seconds):
        logger.info("'%s' is already reachable, no magic packet needed", machine.name, extra=extra)
        event_log.record_action(machine.key, "on", source, "skipped", "already online")
        return
    logger.info("Sending magic packet to '%s' (%s)", machine.name, machine.mac_address, extra=extra)
    send_magic_packet(machine.mac_address)
    event_log.record_action(machine.key, "on", source, "ok")
    schedule_wake_check(scheduler, config, machine, event_log, owners)


def turn_off(
    config: AppConfig,
    machine: Machine,
    event_log: EventLog,
    source: str = "schedule",
    log_owners: tuple[str, ...] = (),
) -> None:
    extra = {"owners": (machine.key, *log_owners)}
    if not is_host_up(machine.ip_address, config.status_check.timeout_seconds):
        logger.info("'%s' is already offline, no shutdown needed", machine.name, extra=extra)
        event_log.record_action(machine.key, "off", source, "skipped", "already offline")
        return

    if machine.shutdown is None:
        logger.warning("'%s' has no shutdown method configured, cannot power it off", machine.name, extra=extra)
        event_log.record_action(machine.key, "off", source, "error", "no shutdown method configured")
        return

    logger.info("Shutting down '%s'", machine.name, extra=extra)
    try:
        _shutdown_client(machine.shutdown).shutdown_node()
    except (requests.RequestException, SshShutdownError) as exc:
        logger.exception("Shutdown failed for '%s'", machine.name, extra=extra)
        event_log.record_action(machine.key, "off", source, "error", str(exc))
        send_notification(config.notifications, f"Shutdown failed for '{machine.name}' ({machine.ip_address}): {exc}")
        return
    event_log.record_action(machine.key, "off", source, "ok")


def _shutdown_client(shutdown: ProxmoxShutdown | SshShutdown):
    if isinstance(shutdown, ProxmoxShutdown):
        return ProxmoxClient(shutdown)
    return SshClient(shutdown)


def cluster_action(
    config: AppConfig,
    cluster: Cluster,
    action: str,
    event_log: EventLog,
    scheduler: BackgroundScheduler,
    source: str = "schedule",
) -> None:
    """Wake the members in order, or shut them down in reverse order, delay_seconds apart.
    Each step is its own scheduler job, so a web request triggering this returns at once."""
    by_key = {m.key: m for m in config.machines}
    members = [by_key[key] for key in cluster.members if key in by_key]
    if action == "off":
        members.reverse()

    cluster_owner = owner_event_key(cluster)
    verb = "waking" if action == "on" else "shutting down"
    logger.info(
        "Cluster '%s': %s %d machine(s), %ds apart", cluster.name, verb, len(members), cluster.delay_seconds,
        extra={"owners": (cluster_owner,)},
    )
    event_log.record_action(
        cluster_owner, action, source, "started", f"{len(members)} machine(s), {cluster.delay_seconds}s apart"
    )

    step_source = f"cluster:{cluster.key}"
    start = datetime.now()
    for i, machine in enumerate(members):
        if action == "on":
            step = functools.partial(
                turn_on, config, machine, event_log, scheduler, step_source, log_owners=(cluster_owner,)
            )
        else:
            step = functools.partial(turn_off, config, machine, event_log, step_source, log_owners=(cluster_owner,))
        # No misfire limit: a step must not be dropped just because the scheduler ran it late.
        scheduler.add_job(
            step,
            trigger="date",
            run_date=start + timedelta(seconds=i * cluster.delay_seconds),
            misfire_grace_time=None,
        )


def on_rule_skipped(
    owner: Machine | Cluster, rule: ScheduleRule, config: AppConfig, config_path: Path, event_log: EventLog
) -> None:
    owner_key = owner_event_key(owner)
    logger.info(
        "Skipping scheduled '%s' for '%s' rule '%s' (skip requested)", rule.action, owner.name, rule.name,
        extra={"owners": (owner_key,)},
    )
    event_log.record_action(owner_key, rule.action, "schedule", "skipped", f"skipped by user ({rule.name})")
    save_config(config, config_path)


def main() -> None:
    config_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent / "config.yaml"
    config = load_config(config_path)
    event_log = EventLog()

    event_handler = EventLogHandler(event_log)
    event_handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(event_handler)

    scheduled_callbacks = {
        "on_action": lambda m: turn_on(config, m, event_log, scheduler, "schedule"),
        "off_action": lambda m: turn_off(config, m, event_log, "schedule"),
        "on_cluster": lambda c, action: cluster_action(config, c, action, event_log, scheduler, "schedule"),
        "on_skip": lambda owner, rule: on_rule_skipped(owner, rule, config, config_path, event_log),
    }
    scheduler = build_scheduler(config, **scheduled_callbacks)
    scheduler.start()
    logger.info(
        "WOL daemon started, %d machine(s) and %d cluster(s) loaded, schedule timezone %s",
        len(config.machines), len(config.clusters), scheduler.timezone,
    )
    if not config.machines:
        logger.warning("No machines configured yet (%s is missing or empty) - add one in the web UI", config_path)

    app = create_app(
        config_path,
        config,
        scheduler,
        event_log,
        wake=lambda m: turn_on(config, m, event_log, scheduler, "manual"),
        shut_down=lambda m: turn_off(config, m, event_log, "manual"),
        run_cluster=lambda c, action: cluster_action(config, c, action, event_log, scheduler, "manual"),
        rebuild_jobs=lambda: update_jobs(scheduler, config, **scheduled_callbacks),
    )

    def handle_shutdown(signum, frame):
        logger.info("Shutting down WOL daemon...")
        scheduler.shutdown()
        sys.exit(0)

    signal.signal(signal.SIGTERM, handle_shutdown)
    signal.signal(signal.SIGINT, handle_shutdown)

    app.run(host="0.0.0.0", port=9090)


if __name__ == "__main__":
    main()
