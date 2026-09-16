from __future__ import annotations

from datetime import date
from typing import Callable

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from .config import ScheduleRule


def build_scheduler(
    rules: list[ScheduleRule],
    on_action: Callable[[], None],
    off_action: Callable[[], None],
    on_skip: Callable[[ScheduleRule], None],
) -> BackgroundScheduler:
    scheduler = BackgroundScheduler()
    update_jobs(scheduler, rules, on_action, off_action, on_skip)
    return scheduler


def update_jobs(
    scheduler: BackgroundScheduler,
    rules: list[ScheduleRule],
    on_action: Callable[[], None],
    off_action: Callable[[], None],
    on_skip: Callable[[ScheduleRule], None],
) -> None:
    for job in scheduler.get_jobs():
        job.remove()
    for index, rule in enumerate(rules):
        hour, minute = rule.time.split(":")
        base_action = on_action if rule.action == "on" else off_action
        trigger = CronTrigger(day_of_week=",".join(rule.days), hour=int(hour), minute=int(minute))
        scheduler.add_job(_gated(rule, base_action, on_skip), trigger=trigger, id=f"rule-{index}", name=rule.name)


def _gated(rule: ScheduleRule, base_action: Callable[[], None], on_skip: Callable[[ScheduleRule], None]):
    def wrapped() -> None:
        if rule.skip_date == date.today().isoformat():
            rule.skip_date = None
            on_skip(rule)
            return
        base_action()

    return wrapped
