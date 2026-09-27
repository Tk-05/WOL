from __future__ import annotations

import re
import secrets
from pathlib import Path
from typing import Callable

from apscheduler.schedulers.background import BackgroundScheduler
from flask import Flask, flash, redirect, render_template, request, url_for

from .config import (
    AppConfig,
    ConfigError,
    KEY_RE,
    Machine,
    ProxmoxShutdown,
    ScheduleRule,
    SshShutdown,
    VALID_ACTIONS,
    VALID_DAYS,
    save_config,
)
from .eventlog import EventLog
from .scheduler import update_jobs
from .status import is_host_up

_TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
_DAY_ORDER = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
_DAY_LABELS = {"mon": "Mon", "tue": "Tue", "wed": "Wed", "thu": "Thu", "fri": "Fri", "sat": "Sat", "sun": "Sun"}


def create_app(
    config_path: Path,
    config: AppConfig,
    scheduler: BackgroundScheduler,
    event_log: EventLog,
    turn_on: Callable[[AppConfig, Machine, EventLog, BackgroundScheduler, str], None],
    turn_off: Callable[[AppConfig, Machine, EventLog, str], None],
    on_rule_skipped: Callable[[Machine, ScheduleRule, AppConfig, Path, EventLog], None],
) -> Flask:
    app = Flask(__name__)
    app.secret_key = secrets.token_hex(16)

    def get_machine(key: str) -> Machine | None:
        return next((m for m in config.machines if m.key == key), None)

    def rebuild_jobs() -> None:
        update_jobs(
            scheduler,
            config.machines,
            on_action=lambda m: turn_on(config, m, event_log, scheduler, "schedule"),
            off_action=lambda m: turn_off(config, m, event_log, "schedule"),
            on_skip=lambda m, rule: on_rule_skipped(m, rule, config, config_path, event_log),
        )

    def next_action_info(machine: Machine):
        prefix = f"{machine.key}:rule-"
        jobs = [
            j for j in scheduler.get_jobs()
            if j.id.startswith(prefix) and getattr(j, "next_run_time", None) is not None
        ]
        if not jobs:
            return None
        job = min(jobs, key=lambda j: j.next_run_time)
        text = f"{job.name} — {job.next_run_time.strftime('%a %d %b %H:%M')}"
        index = int(job.id[len(prefix):])
        if 0 <= index < len(machine.schedule) and machine.schedule[index].skip_date == job.next_run_time.date().isoformat():
            text += " (will be skipped)"
        return text

    def last_action_str(machine: Machine):
        record = event_log.last_action_for(machine.key)
        if record is None:
            return None
        text = f"{record.timestamp:%d %b %H:%M:%S} — {record.action} ({record.source}) — {record.result}"
        if record.detail:
            text += f": {record.detail}"
        return text

    def machine_summary(machine: Machine) -> dict:
        return {
            "key": machine.key,
            "name": machine.name,
            "mac_address": machine.mac_address,
            "ip_address": machine.ip_address,
            "has_shutdown": machine.shutdown is not None,
            "host_up": is_host_up(machine.ip_address, config.status_check.timeout_seconds),
            "last_action": last_action_str(machine),
            "next_action": next_action_info(machine),
        }

    def schedule_view(machine: Machine) -> list[dict]:
        return [
            {
                "index": idx,
                "name": rule.name,
                "days": rule.days,
                "days_str": ", ".join(sorted(rule.days, key=_DAY_ORDER.index)),
                "time": rule.time,
                "action": rule.action,
                "skip_pending": rule.skip_date is not None,
            }
            for idx, rule in enumerate(machine.schedule)
        ]

    @app.route("/")
    def index():
        if len(config.machines) == 1:
            return redirect(url_for("machine_detail", key=config.machines[0].key))
        return render_template(
            "overview.html",
            machines=[machine_summary(m) for m in config.machines],
            events=event_log.recent_events(),
        )

    @app.route("/machines/add", methods=["GET", "POST"])
    def machine_add():
        if request.method == "GET":
            return render_template("add_machine.html")
        try:
            machine = _machine_from_form(request.form, {m.key for m in config.machines})
        except ConfigError as exc:
            flash(str(exc), "error")
            return redirect(url_for("machine_add"))
        config.machines.append(machine)
        save_config(config, config_path)
        rebuild_jobs()
        flash(f"Machine '{machine.name}' added", "success")
        return redirect(url_for("machine_detail", key=machine.key))

    @app.post("/machines/<key>/delete")
    def machine_delete(key: str):
        machine = get_machine(key)
        if machine is None:
            flash("Machine not found", "error")
            return redirect(url_for("index"))
        config.machines.remove(machine)
        save_config(config, config_path)
        rebuild_jobs()
        flash(f"Machine '{machine.name}' deleted", "success")
        return redirect(url_for("index"))

    @app.route("/machines/<key>")
    def machine_detail(key: str):
        machine = get_machine(key)
        if machine is None:
            flash("Machine not found", "error")
            return redirect(url_for("index"))
        return render_template(
            "machine.html",
            machine=machine_summary(machine),
            schedule=schedule_view(machine),
            day_options=[(code, _DAY_LABELS[code]) for code in _DAY_ORDER],
            show_back_link=len(config.machines) > 1,
        )

    @app.post("/machines/<key>/action/on")
    def action_on(key: str):
        machine = get_machine(key)
        if machine is None:
            flash("Machine not found", "error")
            return redirect(url_for("index"))
        turn_on(config, machine, event_log, scheduler, "manual")
        return redirect(url_for("machine_detail", key=key))

    @app.post("/machines/<key>/action/off")
    def action_off(key: str):
        machine = get_machine(key)
        if machine is None:
            flash("Machine not found", "error")
            return redirect(url_for("index"))
        turn_off(config, machine, event_log, "manual")
        return redirect(url_for("machine_detail", key=key))

    @app.post("/machines/<key>/schedule/add")
    def schedule_add(key: str):
        machine = get_machine(key)
        if machine is None:
            flash("Machine not found", "error")
            return redirect(url_for("index"))
        try:
            rule = _rule_from_form(request.form)
        except ConfigError as exc:
            flash(str(exc), "error")
            return redirect(url_for("machine_detail", key=key))
        machine.schedule.append(rule)
        save_config(config, config_path)
        rebuild_jobs()
        flash("Rule added", "success")
        return redirect(url_for("machine_detail", key=key))

    @app.post("/machines/<key>/schedule/<int:index>/edit")
    def schedule_edit(key: str, index: int):
        machine = get_machine(key)
        if machine is None:
            flash("Machine not found", "error")
            return redirect(url_for("index"))
        if not 0 <= index < len(machine.schedule):
            flash("Rule not found", "error")
            return redirect(url_for("machine_detail", key=key))
        try:
            rule = _rule_from_form(request.form)
        except ConfigError as exc:
            flash(str(exc), "error")
            return redirect(url_for("machine_detail", key=key))
        machine.schedule[index] = rule
        save_config(config, config_path)
        rebuild_jobs()
        flash("Rule saved", "success")
        return redirect(url_for("machine_detail", key=key))

    @app.post("/machines/<key>/schedule/<int:index>/delete")
    def schedule_delete(key: str, index: int):
        machine = get_machine(key)
        if machine is None:
            flash("Machine not found", "error")
            return redirect(url_for("index"))
        if 0 <= index < len(machine.schedule):
            del machine.schedule[index]
            save_config(config, config_path)
            rebuild_jobs()
            flash("Rule deleted", "success")
        return redirect(url_for("machine_detail", key=key))

    @app.post("/machines/<key>/schedule/<int:index>/skip")
    def schedule_skip(key: str, index: int):
        machine = get_machine(key)
        if machine is None:
            flash("Machine not found", "error")
            return redirect(url_for("index"))
        if not 0 <= index < len(machine.schedule):
            flash("Rule not found", "error")
            return redirect(url_for("machine_detail", key=key))
        rule = machine.schedule[index]
        if rule.skip_date is not None:
            rule.skip_date = None
            flash("Skip cancelled", "success")
        else:
            job = scheduler.get_job(f"{machine.key}:rule-{index}")
            if job is None or job.next_run_time is None:
                flash("No upcoming run to skip", "error")
                return redirect(url_for("machine_detail", key=key))
            rule.skip_date = job.next_run_time.date().isoformat()
            flash(f"Next run of '{rule.name}' will be skipped", "success")
        save_config(config, config_path)
        return redirect(url_for("machine_detail", key=key))

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


def _machine_from_form(form, existing_keys: set[str]) -> Machine:
    key = form.get("key", "").strip().lower()
    if not key or not KEY_RE.match(key):
        raise ConfigError("Key must use lowercase letters, digits and hyphens only, e.g. 'desktop-pc'")
    if key in existing_keys:
        raise ConfigError(f"A machine with key '{key}' already exists")

    mac_address = form.get("mac_address", "").strip()
    if not mac_address:
        raise ConfigError("MAC address is required")
    ip_address = form.get("ip_address", "").strip()
    if not ip_address:
        raise ConfigError("IP address is required")
    name = form.get("name", "").strip() or key

    shutdown_type = form.get("shutdown_type", "none")
    shutdown: ProxmoxShutdown | SshShutdown | None = None
    if shutdown_type == "proxmox":
        host = form.get("proxmox_host", "").strip()
        node = form.get("proxmox_node", "").strip()
        token_id = form.get("proxmox_token_id", "").strip()
        token_secret = form.get("proxmox_token_secret", "").strip()
        if not (host and node and token_id and token_secret):
            raise ConfigError("Proxmox host, node, token id and token secret are all required")
        shutdown = ProxmoxShutdown(
            host=host.rstrip("/"),
            node=node,
            token_id=token_id,
            token_secret=token_secret,
            verify_ssl=form.get("proxmox_verify_ssl") == "on",
        )
    elif shutdown_type == "ssh":
        ssh_host = form.get("ssh_host", "").strip() or ip_address
        ssh_port_raw = form.get("ssh_port", "").strip()
        try:
            ssh_port = int(ssh_port_raw) if ssh_port_raw else 22
        except ValueError:
            raise ConfigError("SSH port must be a number")
        shutdown = SshShutdown(
            host=ssh_host,
            port=ssh_port,
            username=form.get("ssh_username", "").strip() or "root",
            private_key_path=form.get("ssh_private_key_path", "").strip(),
            command=form.get("ssh_command", "").strip() or "shutdown -h +1",
        )
    elif shutdown_type != "none":
        raise ConfigError(f"Invalid shutdown type: {shutdown_type}")

    return Machine(key=key, name=name, mac_address=mac_address, ip_address=ip_address, shutdown=shutdown, schedule=[])
