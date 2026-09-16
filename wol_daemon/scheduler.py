from __future__ import annotations

from typing import Callable

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from .config import ScheduleRule


def build_scheduler(
    rules: list[ScheduleRule],
    on_action: Callable[[], None],
    off_action: Callable[[], None],
) -> BackgroundScheduler:
    scheduler = BackgroundScheduler()
    update_jobs(scheduler, rules, on_action, off_action)
    return scheduler


def update_jobs(
    scheduler: BackgroundScheduler,
    rules: list[ScheduleRule],
    on_action: Callable[[], None],
    off_action: Callable[[], None],
) -> None:
    for job in scheduler.get_jobs():
        job.remove()
    for rule in rules:
        hour, minute = rule.time.split(":")
        action = on_action if rule.action == "on" else off_action
        trigger = CronTrigger(day_of_week=",".join(rule.days), hour=int(hour), minute=int(minute))
        scheduler.add_job(action, trigger=trigger, name=rule.name)
