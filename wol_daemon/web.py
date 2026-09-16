from __future__ import annotations

import re
import secrets
from pathlib import Path
from typing import Callable

from apscheduler.schedulers.background import BackgroundScheduler
from flask import Flask, flash, redirect, render_template, request, url_for

from .config import AppConfig, ConfigError, ScheduleRule, VALID_ACTIONS, VALID_DAYS, save_config
from .eventlog import EventLog
from .proxmox import ProxmoxClient
from .scheduler import update_jobs
from .status import is_host_up

_TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
_DAY_ORDER = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
_DAY_LABELS = {"mon": "Mon", "tue": "Tue", "wed": "Wed", "thu": "Thu", "fri": "Fri", "sat": "Sat", "sun": "Sun"}


def create_app(
    config_path: Path,
    config: AppConfig,
    scheduler: BackgroundScheduler,
    proxmox_client: ProxmoxClient,
    event_log: EventLog,
    turn_on: Callable[[AppConfig, EventLog, BackgroundScheduler, str], None],
    turn_off: Callable[[AppConfig, ProxmoxClient, EventLog, str], None],
) -> Flask:
    app = Flask(__name__)
    app.secret_key = secrets.token_hex(16)

    def rebuild_jobs() -> None:
        update_jobs(
            scheduler,
            config.schedule,
            on_action=lambda: turn_on(config, event_log, scheduler, "schedule"),
            off_action=lambda: turn_off(config, proxmox_client, event_log, "schedule"),
        )

    def next_action_info():
        jobs = [j for j in scheduler.get_jobs() if getattr(j, "next_run_time", None) is not None]
        if not jobs:
            return None
        job = min(jobs, key=lambda j: j.next_run_time)
        return job.name, job.next_run_time.strftime("%a %d %b %H:%M")

    def last_action_str():
        record = event_log.last_action
        if record is None:
            return None
        text = f"{record.timestamp:%d %b %H:%M:%S} — {record.action} ({record.source}) — {record.result}"
        if record.detail:
            text += f": {record.detail}"
        return text

    def schedule_view():
        return [
            {
                "index": idx,
                "name": rule.name,
                "days": rule.days,
                "days_str": ", ".join(sorted(rule.days, key=_DAY_ORDER.index)),
                "time": rule.time,
                "action": rule.action,
            }
            for idx, rule in enumerate(config.schedule)
        ]

    @app.route("/")
    def index():
        return render_template(
            "index.html",
            host_up=is_host_up(config.target.ip_address, config.status_check.timeout_seconds),
            target=config.target,
            schedule=schedule_view(),
            last_action=last_action_str(),
            next_action=next_action_info(),
            events=event_log.recent_events(),
            day_options=[(code, _DAY_LABELS[code]) for code in _DAY_ORDER],
        )

    @app.post("/action/on")
    def action_on():
        turn_on(config, event_log, scheduler, "manual")
        return redirect(url_for("index"))

    @app.post("/action/off")
    def action_off():
        turn_off(config, proxmox_client, event_log, "manual")
        return redirect(url_for("index"))

    @app.post("/schedule/add")
    def schedule_add():
        try:
            rule = _rule_from_form(request.form)
        except ConfigError as exc:
            flash(str(exc), "error")
            return redirect(url_for("index"))
        config.schedule.append(rule)
        save_config(config, config_path)
        rebuild_jobs()
        flash("Rule added", "success")
        return redirect(url_for("index"))

    @app.post("/schedule/<int:index>/edit")
    def schedule_edit(index: int):
        if not 0 <= index < len(config.schedule):
            flash("Rule not found", "error")
            return redirect(url_for("index"))
        try:
            rule = _rule_from_form(request.form)
        except ConfigError as exc:
            flash(str(exc), "error")
            return redirect(url_for("index"))
        config.schedule[index] = rule
        save_config(config, config_path)
        rebuild_jobs()
        flash("Rule saved", "success")
        return redirect(url_for("index"))

    @app.post("/schedule/<int:index>/delete")
    def schedule_delete(index: int):
        if 0 <= index < len(config.schedule):
            del config.schedule[index]
            save_config(config, config_path)
            rebuild_jobs()
            flash("Rule deleted", "success")
        return redirect(url_for("index"))

    return app


def _rule_from_form(form) -> ScheduleRule:
    days = form.getlist("days")
    invalid_days = set(days) - VALID_DAYS
    if not days or invalid_days:
        raise ConfigError("Please select at least one valid day")
    action = form.get("action", "")
    if action not in VALID_ACTIONS:
        raise ConfigError(f"Invalid action: {action}")
    time_value = form.get("time", "")
    if not _TIME_RE.match(time_value):
        raise ConfigError("Time must be in HH:MM format")
    name = form.get("name", "").strip() or f"{action.capitalize()} rule"
    return ScheduleRule(name=name, days=days, time=time_value, action=action)
