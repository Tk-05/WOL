from __future__ import annotations

from datetime import date
from typing import Callable

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from .config import Machine, ScheduleRule


def build_scheduler(
    machines: list[Machine],
    on_action: Callable[[Machine], None],
    off_action: Callable[[Machine], None],
    on_skip: Callable[[Machine, ScheduleRule], None],
) -> BackgroundScheduler:
    scheduler = BackgroundScheduler()
    update_jobs(scheduler, machines, on_action, off_action, on_skip)
    return scheduler


def update_jobs(
    scheduler: BackgroundScheduler,
    machines: list[Machine],
    on_action: Callable[[Machine], None],
    off_action: Callable[[Machine], None],
    on_skip: Callable[[Machine, ScheduleRule], None],
) -> None:
    for job in scheduler.get_jobs():
        job.remove()
    for machine in machines:
        for index, rule in enumerate(machine.schedule):
            hour, minute = rule.time.split(":")
            base_action = _bind(on_action, machine) if rule.action == "on" else _bind(off_action, machine)
            trigger = CronTrigger(day_of_week=",".join(rule.days), hour=int(hour), minute=int(minute))
            scheduler.add_job(
                _gated(machine, rule, base_action, on_skip),
                trigger=trigger,
                id=f"{machine.key}:rule-{index}",
                name=rule.name,
            )


def _bind(action: Callable[[Machine], None], machine: Machine) -> Callable[[], None]:
    return lambda: action(machine)


def _gated(
    machine: Machine,
    rule: ScheduleRule,
    base_action: Callable[[], None],
    on_skip: Callable[[Machine, ScheduleRule], None],
):
    def wrapped() -> None:
        if rule.skip_date == date.today().isoformat():
            rule.skip_date = None
            on_skip(machine, rule)
            return
        base_action()

    return wrapped
