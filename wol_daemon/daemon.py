from __future__ import annotations

import logging
import signal
import sys
import threading
import time
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


# One running wake/shutdown sequence per cluster, keyed by cluster key. Starting a new one
# cancels the old one, so e.g. "Shut down all" isn't stuck behind a slow "Wake all".
_running_sequences: dict[str, threading.Event] = {}
_sequences_lock = threading.Lock()
POLL_SECONDS = 5


def cluster_action(
    config: AppConfig,
    cluster: Cluster,
    action: str,
    event_log: EventLog,
    scheduler: BackgroundScheduler,
    source: str = "schedule",
) -> None:
    """Wake the members in order, or shut them down in reverse order. Unless max_wait_seconds
    is 0, each next machine only starts once the previous one is up (or down), or once
    max_wait_seconds have passed; then delay_seconds more. Runs in its own thread, since a
    sequence can take minutes, so a web request triggering it returns at once."""
    by_key = {m.key: m for m in config.machines}
    members = [by_key[key] for key in cluster.members if key in by_key]
    if action == "off":
        members.reverse()

    cancel = threading.Event()
    with _sequences_lock:
        previous = _running_sequences.get(cluster.key)
        if previous is not None:
            previous.set()
        _running_sequences[cluster.key] = cancel

    cluster_owner = owner_event_key(cluster)
    extra = {"owners": (cluster_owner,)}
    if previous is not None:
        logger.info("Cluster '%s': previous sequence cancelled", cluster.name, extra=extra)
    verb = "waking" if action == "on" else "shutting down"
    plan = _describe_sequence(cluster, action, len(members))
    logger.info("Cluster '%s': %s %s", cluster.name, verb, plan, extra=extra)
    event_log.record_action(cluster_owner, action, source, "started", plan)

    threading.Thread(
        target=_run_sequence,
        args=(config, cluster, action, members, event_log, scheduler, source, cancel),
        name=f"cluster-{cluster.key}",
        daemon=True,
    ).start()


def _describe_sequence(cluster: Cluster, action: str, count: int) -> str:
    if cluster.max_wait_seconds == 0:
        return f"{count} machine(s), {cluster.delay_seconds}s apart"
    state = "online" if action == "on" else "off"
    return (
        f"{count} machine(s), each after the previous is {state} "
        f"(max {cluster.max_wait_seconds}s) plus {cluster.delay_seconds}s"
    )


def _run_sequence(
    config: AppConfig,
    cluster: Cluster,
    action: str,
    members: list[Machine],
    event_log: EventLog,
    scheduler: BackgroundScheduler,
    source: str,
    cancel: threading.Event,
) -> None:
    cluster_owner = owner_event_key(cluster)
    want_up = action == "on"
    state = "online" if want_up else "off"
    step_source = f"cluster:{cluster.key}"
    problems: list[str] = []
    started = time.monotonic()
    try:
        for i, machine in enumerate(members):
            if cancel.is_set():
                event_log.record_action(cluster_owner, action, source, "cancelled", "replaced by a newer action")
                return
            if want_up:
                turn_on(config, machine, event_log, scheduler, step_source, log_owners=(cluster_owner,))
            else:
                turn_off(config, machine, event_log, step_source, log_owners=(cluster_owner,))

            record = event_log.last_action_for(machine.key)
            if record is not None and record.result == "error":
                problems.append(machine.name)
            if i == len(members) - 1:
                break

            both = {"owners": (cluster_owner, machine.key)}
            if cluster.max_wait_seconds > 0 and record is not None and record.result == "ok":
                waited = _wait_for_state(config, machine, want_up, cluster.max_wait_seconds, cancel)
                if waited is not None:
                    logger.info("'%s' is %s after %ds", machine.name, state, waited, extra=both)
                elif not cancel.is_set():
                    logger.warning(
                        "'%s' still not %s after %ds, continuing with the next machine",
                        machine.name, state, cluster.max_wait_seconds, extra=both,
                    )
                    problems.append(machine.name)
            if cancel.wait(cluster.delay_seconds):
                continue  # the check at the top of the loop records the cancellation

        elapsed = int(time.monotonic() - started)
        if problems:
            detail = f"finished after {elapsed}s; problems with: {', '.join(problems)}"
        else:
            detail = f"all {len(members)} machine(s) done after {elapsed}s"
        logger.info("Cluster '%s': %s", cluster.name, detail, extra={"owners": (cluster_owner,)})
        event_log.record_action(cluster_owner, action, source, "error" if problems else "ok", detail)
    finally:
        with _sequences_lock:
            if _running_sequences.get(cluster.key) is cancel:
                del _running_sequences[cluster.key]


def _wait_for_state(
    config: AppConfig, machine: Machine, want_up: bool, max_wait: int, cancel: threading.Event
) -> int | None:
    """Seconds until the machine answered pings (want_up) or stopped answering; None if it
    didn't within max_wait, or the sequence was cancelled meanwhile."""
    start = time.monotonic()
    while True:
        if is_host_up(machine.ip_address, config.status_check.timeout_seconds) == want_up:
            return int(time.monotonic() - start)
        remaining = max_wait - (time.monotonic() - start)
        if remaining <= 0 or cancel.wait(min(POLL_SECONDS, remaining)):
            return None


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
    event_log = EventLog(path=config_path.parent / "events.jsonl")

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
