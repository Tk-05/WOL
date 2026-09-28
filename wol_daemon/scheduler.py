from __future__ import annotations

from datetime import date
from typing import Callable

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from .config import AppConfig, Cluster, Machine, ScheduleRule

# Rule job ids look like "<machine-key>:rule-<i>" or "cluster@<cluster-key>:rule-<i>". Keys
# can't contain "@" or ":", so the two kinds can never collide. One-off jobs (wake checks,
# cluster steps) get APScheduler's random ids and are left alone when rules are rebuilt.
RULE_MARKER = ":rule-"


def machine_job_prefix(machine: Machine) -> str:
    return f"{machine.key}{RULE_MARKER}"


def cluster_job_prefix(cluster: Cluster) -> str:
    return f"cluster@{cluster.key}{RULE_MARKER}"


def owner_event_key(owner: Machine | Cluster) -> str:
    """Key under which the event log tracks an owner's last action; machine and cluster
    keys live in separate namespaces, so clusters get a prefix."""
    return f"cluster@{owner.key}" if isinstance(owner, Cluster) else owner.key


def build_scheduler(
    config: AppConfig,
    on_action: Callable[[Machine], None],
    off_action: Callable[[Machine], None],
    on_cluster: Callable[[Cluster, str], None],
    on_skip: Callable[[Machine | Cluster, ScheduleRule], None],
) -> BackgroundScheduler:
    scheduler = BackgroundScheduler()
    update_jobs(scheduler, config, on_action, off_action, on_cluster, on_skip)
    return scheduler


def update_jobs(
    scheduler: BackgroundScheduler,
    config: AppConfig,
    on_action: Callable[[Machine], None],
    off_action: Callable[[Machine], None],
    on_cluster: Callable[[Cluster, str], None],
    on_skip: Callable[[Machine | Cluster, ScheduleRule], None],
) -> None:
    for job in scheduler.get_jobs():
        if RULE_MARKER in job.id:
            job.remove()

    for machine in config.machines:
        for index, rule in enumerate(machine.schedule):
            action = on_action if rule.action == "on" else off_action
            _add_rule_job(scheduler, machine_job_prefix(machine) + str(index), machine, rule, _bind(action, machine), on_skip)

    for cluster in config.clusters:
        for index, rule in enumerate(cluster.schedule):
            run = _bind_cluster(on_cluster, cluster, rule.action)
            _add_rule_job(scheduler, cluster_job_prefix(cluster) + str(index), cluster, rule, run, on_skip)


def _add_rule_job(scheduler, job_id, owner, rule: ScheduleRule, base_action, on_skip) -> None:
    hour, minute = rule.time.split(":")
    trigger = CronTrigger(day_of_week=",".join(rule.days), hour=int(hour), minute=int(minute))
    scheduler.add_job(_gated(owner, rule, base_action, on_skip), trigger=trigger, id=job_id, name=rule.name)


def _bind(action: Callable[[Machine], None], machine: Machine) -> Callable[[], None]:
    return lambda: action(machine)


def _bind_cluster(on_cluster: Callable[[Cluster, str], None], cluster: Cluster, action: str) -> Callable[[], None]:
    return lambda: on_cluster(cluster, action)


def _gated(
    owner: Machine | Cluster,
    rule: ScheduleRule,
    base_action: Callable[[], None],
    on_skip: Callable[[Machine | Cluster, ScheduleRule], None],
):
    def wrapped() -> None:
        if rule.skip_date == date.today().isoformat():
            rule.skip_date = None
            on_skip(owner, rule)
            return
        base_action()

    return wrapped
