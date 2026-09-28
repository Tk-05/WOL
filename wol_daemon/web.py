from __future__ import annotations

import re
import secrets
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path
from typing import Callable

from apscheduler.schedulers.background import BackgroundScheduler
from flask import Flask, Response, flash, redirect, render_template, request, url_for

from .config import (
    AppConfig,
    Cluster,
    ConfigError,
    ENV_MACHINE_KEY,
    KEY_RE,
    Machine,
    ProxmoxShutdown,
    ScheduleRule,
    SshShutdown,
    VALID_ACTIONS,
    VALID_DAYS,
    config_to_yaml,
    env_config_active,
    load_config,
    parse_config_text,
    save_config,
)
from .eventlog import EventLog
from .scheduler import cluster_job_prefix, machine_job_prefix, owner_event_key
from .status import is_host_up

_TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
_MAC_RE = re.compile(r"^[0-9A-Fa-f]{2}([:-]?[0-9A-Fa-f]{2}){5}$")
_DAY_ORDER = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
_DAY_LABELS = {"mon": "Mon", "tue": "Tue", "wed": "Wed", "thu": "Thu", "fri": "Fri", "sat": "Sat", "sun": "Sun"}
_DAY_OPTIONS = [(code, _DAY_LABELS[code]) for code in _DAY_ORDER]


def create_app(
    config_path: Path,
    config: AppConfig,
    scheduler: BackgroundScheduler,
    event_log: EventLog,
    wake: Callable[[Machine], None],
    shut_down: Callable[[Machine], None],
    run_cluster: Callable[[Cluster, str], None],
    rebuild_jobs: Callable[[], None],
) -> Flask:
    app = Flask(__name__)
    app.secret_key = secrets.token_hex(16)
    app.config["MAX_CONTENT_LENGTH"] = 1024 * 1024  # config imports are tiny; refuse anything big

    def get_machine(key: str) -> Machine | None:
        return next((m for m in config.machines if m.key == key), None)

    def get_cluster(key: str) -> Cluster | None:
        return next((c for c in config.clusters if c.key == key), None)

    def is_env_machine(machine: Machine) -> bool:
        # WOL_* env vars rebuild this machine on every start, so UI edits would be lost.
        return machine.key == ENV_MACHINE_KEY and env_config_active()

    def ping_all(machines: list[Machine]) -> dict[str, bool]:
        if not machines:
            return {}
        timeout = config.status_check.timeout_seconds
        with ThreadPoolExecutor(max_workers=min(16, len(machines))) as pool:
            results = list(pool.map(lambda m: is_host_up(m.ip_address, timeout), machines))
        return {m.key: up for m, up in zip(machines, results)}

    def next_action_info(prefix: str, schedule: list[ScheduleRule]) -> str | None:
        jobs = [
            j for j in scheduler.get_jobs()
            if j.id.startswith(prefix) and getattr(j, "next_run_time", None) is not None
        ]
        if not jobs:
            return None
        job = min(jobs, key=lambda j: j.next_run_time)
        text = f"{job.name} — {job.next_run_time.strftime('%a %d %b %H:%M')}"
        index = int(job.id[len(prefix):])
        if 0 <= index < len(schedule) and schedule[index].skip_date == job.next_run_time.date().isoformat():
            text += " (will be skipped)"
        return text

    def last_action_str(event_key: str) -> str | None:
        record = event_log.last_action_for(event_key)
        if record is None:
            return None
        text = f"{record.timestamp:%d %b %H:%M:%S} — {record.action} ({record.source}) — {record.result}"
        if record.detail:
            text += f": {record.detail}"
        return text

    def machine_summary(machine: Machine, host_up: bool) -> dict:
        return {
            "key": machine.key,
            "name": machine.name,
            "mac_address": machine.mac_address,
            "ip_address": machine.ip_address,
            "has_shutdown": machine.shutdown is not None,
            "host_up": host_up,
            "last_action": last_action_str(machine.key),
            "next_action": next_action_info(machine_job_prefix(machine), machine.schedule),
            "clusters": [{"key": c.key, "name": c.name} for c in config.clusters if machine.key in c.members],
        }

    def cluster_summary(cluster: Cluster, status: dict[str, bool]) -> dict:
        members = [m for m in (get_machine(key) for key in cluster.members) if m is not None]
        return {
            "key": cluster.key,
            "name": cluster.name,
            "delay_seconds": cluster.delay_seconds,
            "members": [{"key": m.key, "name": m.name, "host_up": status.get(m.key, False)} for m in members],
            "online": sum(1 for m in members if status.get(m.key)),
            "last_action": last_action_str(owner_event_key(cluster)),
            "next_action": next_action_info(cluster_job_prefix(cluster), cluster.schedule),
        }

    def schedule_view(schedule: list[ScheduleRule]) -> list[dict]:
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
            for idx, rule in enumerate(schedule)
        ]

    def machine_choices() -> list[dict]:
        return [{"key": m.key, "name": m.name} for m in config.machines]

    # --- Schedule editing, shared by machines and clusters ---

    def add_rule(owner: Machine | Cluster, back: str):
        try:
            rule = _rule_from_form(request.form)
        except ConfigError as exc:
            flash(str(exc), "error")
            return redirect(back)
        owner.schedule.append(rule)
        save_config(config, config_path)
        rebuild_jobs()
        flash("Rule added", "success")
        return redirect(back)

    def edit_rule(owner: Machine | Cluster, index: int, back: str):
        if not 0 <= index < len(owner.schedule):
            flash("Rule not found", "error")
            return redirect(back)
        try:
            rule = _rule_from_form(request.form)
        except ConfigError as exc:
            flash(str(exc), "error")
            return redirect(back)
        owner.schedule[index] = rule
        save_config(config, config_path)
        rebuild_jobs()
        flash("Rule saved", "success")
        return redirect(back)

    def delete_rule(owner: Machine | Cluster, index: int, back: str):
        if 0 <= index < len(owner.schedule):
            del owner.schedule[index]
            save_config(config, config_path)
            rebuild_jobs()
            flash("Rule deleted", "success")
        return redirect(back)

    def skip_rule(owner: Machine | Cluster, index: int, job_id: str, back: str):
        if not 0 <= index < len(owner.schedule):
            flash("Rule not found", "error")
            return redirect(back)
        rule = owner.schedule[index]
        if rule.skip_date is not None:
            rule.skip_date = None
            flash("Skip cancelled", "success")
        else:
            job = scheduler.get_job(job_id)
            if job is None or job.next_run_time is None:
                flash("No upcoming run to skip", "error")
                return redirect(back)
            rule.skip_date = job.next_run_time.date().isoformat()
            flash(f"Next run of '{rule.name}' will be skipped", "success")
        save_config(config, config_path)
        return redirect(back)

    def machine_or_redirect(key: str) -> Machine | None:
        machine = get_machine(key)
        if machine is None:
            flash("Machine not found", "error")
        return machine

    def cluster_or_redirect(key: str) -> Cluster | None:
        cluster = get_cluster(key)
        if cluster is None:
            flash("Cluster not found", "error")
        return cluster

    # --- Overview ---

    @app.route("/")
    def index():
        if len(config.machines) == 1 and not config.clusters:
            return redirect(url_for("machine_detail", key=config.machines[0].key))
        status = ping_all(config.machines)
        return render_template(
            "overview.html",
            machines=[machine_summary(m, status[m.key]) for m in config.machines],
            clusters=[cluster_summary(c, status) for c in config.clusters],
            events=event_log.recent_events(),
        )

    # --- Machines ---

    @app.route("/machines/add", methods=["GET", "POST"])
    def machine_add():
        if request.method == "GET":
            return render_template("add_machine.html", form=_machine_form_values(None))
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
        machine = machine_or_redirect(key)
        if machine is None:
            return redirect(url_for("index"))
        config.machines.remove(machine)
        for cluster in config.clusters:
            if key in cluster.members:
                cluster.members.remove(key)
        save_config(config, config_path)
        rebuild_jobs()
        flash(f"Machine '{machine.name}' deleted", "success")
        return redirect(url_for("index"))

    @app.route("/machines/<key>")
    def machine_detail(key: str):
        machine = machine_or_redirect(key)
        if machine is None:
            return redirect(url_for("index"))
        status = ping_all([machine])
        return render_template(
            "machine.html",
            machine=machine_summary(machine, status[machine.key]),
            schedule=schedule_view(machine.schedule),
            day_options=_DAY_OPTIONS,
            owner_key=machine.key,
            sched_ep="schedule",
            form=_machine_form_values(machine),
            env_locked=is_env_machine(machine),
        )

    @app.post("/machines/<key>/edit")
    def machine_edit(key: str):
        machine = machine_or_redirect(key)
        if machine is None:
            return redirect(url_for("index"))
        back = url_for("machine_detail", key=key)
        if is_env_machine(machine):
            flash("This machine comes from WOL_* environment variables; change them in the stack instead", "error")
            return redirect(back)
        try:
            name, mac_address, ip_address, shutdown = _machine_settings_from_form(request.form, key, machine.shutdown)
        except ConfigError as exc:
            flash(str(exc), "error")
            return redirect(back)
        # Update the existing object instead of replacing it: scheduled rule jobs, pending wake
        # checks and running cluster steps all hold a reference to it, and must see the change.
        machine.name = name
        machine.mac_address = mac_address
        machine.ip_address = ip_address
        machine.shutdown = shutdown
        save_config(config, config_path)
        flash(f"Machine '{machine.name}' saved", "success")
        return redirect(back)

    @app.post("/machines/<key>/action/<action>")
    def machine_action(key: str, action: str):
        machine = machine_or_redirect(key)
        if machine is None:
            return redirect(url_for("index"))
        if action == "on":
            wake(machine)
        elif action == "off":
            shut_down(machine)
        return redirect(url_for("machine_detail", key=key))

    @app.post("/machines/<key>/schedule/add")
    def schedule_add(key: str):
        machine = machine_or_redirect(key)
        if machine is None:
            return redirect(url_for("index"))
        return add_rule(machine, url_for("machine_detail", key=key))

    @app.post("/machines/<key>/schedule/<int:index>/edit")
    def schedule_edit(key: str, index: int):
        machine = machine_or_redirect(key)
        if machine is None:
            return redirect(url_for("index"))
        return edit_rule(machine, index, url_for("machine_detail", key=key))

    @app.post("/machines/<key>/schedule/<int:index>/delete")
    def schedule_delete(key: str, index: int):
        machine = machine_or_redirect(key)
        if machine is None:
            return redirect(url_for("index"))
        return delete_rule(machine, index, url_for("machine_detail", key=key))

    @app.post("/machines/<key>/schedule/<int:index>/skip")
    def schedule_skip(key: str, index: int):
        machine = machine_or_redirect(key)
        if machine is None:
            return redirect(url_for("index"))
        return skip_rule(machine, index, machine_job_prefix(machine) + str(index), url_for("machine_detail", key=key))

    # --- Clusters ---

    @app.route("/clusters/add", methods=["GET", "POST"])
    def cluster_add():
        if request.method == "GET":
            return render_template(
                "add_cluster.html", machines=machine_choices(), cluster_name="", delay_seconds=60, positions={}
            )
        try:
            key = request.form.get("key", "").strip().lower()
            if not key or not KEY_RE.match(key):
                raise ConfigError("Key must use lowercase letters, digits and hyphens only, e.g. 'homelab'")
            if get_cluster(key) is not None:
                raise ConfigError(f"A cluster with key '{key}' already exists")
            members, delay = _cluster_members_from_form(request.form, config.machines)
        except ConfigError as exc:
            flash(str(exc), "error")
            return redirect(url_for("cluster_add"))
        name = request.form.get("name", "").strip() or key
        cluster = Cluster(key=key, name=name, members=members, delay_seconds=delay, schedule=[])
        config.clusters.append(cluster)
        save_config(config, config_path)
        flash(f"Cluster '{cluster.name}' added", "success")
        return redirect(url_for("cluster_detail", key=cluster.key))

    @app.route("/clusters/<key>")
    def cluster_detail(key: str):
        cluster = cluster_or_redirect(key)
        if cluster is None:
            return redirect(url_for("index"))
        status = ping_all([m for m in config.machines if m.key in cluster.members])
        return render_template(
            "cluster.html",
            cluster=cluster_summary(cluster, status),
            schedule=schedule_view(cluster.schedule),
            day_options=_DAY_OPTIONS,
            machines=machine_choices(),
            positions={member: i + 1 for i, member in enumerate(cluster.members)},
            cluster_name=cluster.name,
            delay_seconds=cluster.delay_seconds,
            owner_key=cluster.key,
            sched_ep="cluster_schedule",
        )

    @app.post("/clusters/<key>/edit")
    def cluster_edit(key: str):
        cluster = cluster_or_redirect(key)
        if cluster is None:
            return redirect(url_for("index"))
        try:
            members, delay = _cluster_members_from_form(request.form, config.machines)
        except ConfigError as exc:
            flash(str(exc), "error")
            return redirect(url_for("cluster_detail", key=key))
        cluster.name = request.form.get("name", "").strip() or cluster.name
        cluster.members = members
        cluster.delay_seconds = delay
        save_config(config, config_path)
        flash("Cluster saved", "success")
        return redirect(url_for("cluster_detail", key=key))

    @app.post("/clusters/<key>/delete")
    def cluster_delete(key: str):
        cluster = cluster_or_redirect(key)
        if cluster is None:
            return redirect(url_for("index"))
        config.clusters.remove(cluster)
        save_config(config, config_path)
        rebuild_jobs()
        flash(f"Cluster '{cluster.name}' deleted", "success")
        return redirect(url_for("index"))

    @app.post("/clusters/<key>/action/<action>")
    def cluster_action(key: str, action: str):
        cluster = cluster_or_redirect(key)
        if cluster is None:
            return redirect(url_for("index"))
        if action in VALID_ACTIONS:
            run_cluster(cluster, action)
            verb = "Waking" if action == "on" else "Shutting down"
            flash(
                f"{verb} {len(cluster.members)} machine(s), {cluster.delay_seconds}s apart — see the event log",
                "success",
            )
        return redirect(url_for("cluster_detail", key=key))

    @app.post("/clusters/<key>/schedule/add")
    def cluster_schedule_add(key: str):
        cluster = cluster_or_redirect(key)
        if cluster is None:
            return redirect(url_for("index"))
        return add_rule(cluster, url_for("cluster_detail", key=key))

    @app.post("/clusters/<key>/schedule/<int:index>/edit")
    def cluster_schedule_edit(key: str, index: int):
        cluster = cluster_or_redirect(key)
        if cluster is None:
            return redirect(url_for("index"))
        return edit_rule(cluster, index, url_for("cluster_detail", key=key))

    @app.post("/clusters/<key>/schedule/<int:index>/delete")
    def cluster_schedule_delete(key: str, index: int):
        cluster = cluster_or_redirect(key)
        if cluster is None:
            return redirect(url_for("index"))
        return delete_rule(cluster, index, url_for("cluster_detail", key=key))

    @app.post("/clusters/<key>/schedule/<int:index>/skip")
    def cluster_schedule_skip(key: str, index: int):
        cluster = cluster_or_redirect(key)
        if cluster is None:
            return redirect(url_for("index"))
        return skip_rule(cluster, index, cluster_job_prefix(cluster) + str(index), url_for("cluster_detail", key=key))

    # --- Import / export ---

    @app.route("/config")
    def config_page():
        return render_template("config.html", env_active=env_config_active())

    @app.route("/config/export")
    def config_export():
        filename = f"wol-config-{date.today().isoformat()}.yaml"
        return Response(
            config_to_yaml(config),
            mimetype="application/x-yaml",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    @app.post("/config/import")
    def config_import():
        upload = request.files.get("file")
        if upload is None or not upload.filename:
            flash("Choose a file to import", "error")
            return redirect(url_for("config_page"))
        try:
            imported = parse_config_text(upload.read().decode("utf-8-sig"))
        except UnicodeDecodeError:
            flash("Import failed, nothing was changed: the file isn't UTF-8 text", "error")
            return redirect(url_for("config_page"))
        except ConfigError as exc:
            flash(f"Import failed, nothing was changed: {exc}", "error")
            return redirect(url_for("config_page"))

        save_config(imported, config_path)
        # Reload from disk so WOL_* environment variables are applied the same way as on startup.
        reloaded = load_config(config_path)
        config.machines = reloaded.machines
        config.clusters = reloaded.clusters
        config.status_check = reloaded.status_check
        config.wol = reloaded.wol
        config.notifications = reloaded.notifications
        rebuild_jobs()
        flash(
            f"Imported {len(config.machines)} machine(s) and {len(config.clusters)} cluster(s). "
            "The previous config.yaml was kept in backups/.",
            "success",
        )
        return redirect(url_for("index"))

    @app.errorhandler(413)
    def upload_too_large(_error):
        flash("Import failed, nothing was changed: the file is larger than 1 MB", "error")
        return redirect(url_for("config_page"))

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


def _cluster_members_from_form(form, machines: list[Machine]) -> tuple[list[str], int]:
    """Members come from one position field per machine (empty = not a member). Ties keep
    the machine list order, so '1, 1, 2' is fine and doesn't need renumbering."""
    positioned = []
    for list_index, machine in enumerate(machines):
        raw = form.get(f"pos_{machine.key}", "").strip()
        if not raw:
            continue
        try:
            position = int(raw)
        except ValueError:
            raise ConfigError(f"Position for '{machine.name}' must be a whole number")
        if position < 1:
            raise ConfigError(f"Position for '{machine.name}' must be 1 or higher")
        positioned.append((position, list_index, machine.key))
    if not positioned:
        raise ConfigError("Give at least one machine a position to put it in the cluster")

    delay_raw = form.get("delay_seconds", "").strip()
    try:
        delay = int(delay_raw) if delay_raw else 60
    except ValueError:
        raise ConfigError("Pause must be a whole number of seconds")
    if delay < 0:
        raise ConfigError("Pause can't be negative")

    return [key for _, _, key in sorted(positioned)], delay


def _machine_from_form(form, existing_keys: set[str]) -> Machine:
    key = form.get("key", "").strip().lower()
    if not key or not KEY_RE.match(key):
        raise ConfigError("Key must use lowercase letters, digits and hyphens only, e.g. 'desktop-pc'")
    if key in existing_keys:
        raise ConfigError(f"A machine with key '{key}' already exists")
    name, mac_address, ip_address, shutdown = _machine_settings_from_form(form, key, previous_shutdown=None)
    return Machine(key=key, name=name, mac_address=mac_address, ip_address=ip_address, shutdown=shutdown, schedule=[])


def _machine_settings_from_form(
    form, key: str, previous_shutdown: ProxmoxShutdown | SshShutdown | None
) -> tuple[str, str, str, ProxmoxShutdown | SshShutdown | None]:
    """Shared by adding and editing. The Proxmox token secret is never sent to the browser,
    so an empty secret field while editing means 'keep the current one'."""
    mac_address = form.get("mac_address", "").strip()
    if not _MAC_RE.match(mac_address):
        raise ConfigError("MAC address must look like AA:BB:CC:DD:EE:FF")
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
        if not token_secret and isinstance(previous_shutdown, ProxmoxShutdown):
            token_secret = previous_shutdown.token_secret
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

    return name, mac_address, ip_address, shutdown


def _machine_form_values(machine: Machine | None) -> dict:
    """Prefill values for the shared machine form; empty for 'Add machine'."""
    values = {
        "name": "", "mac_address": "", "ip_address": "", "shutdown_type": "none",
        "proxmox_host": "", "proxmox_node": "", "proxmox_token_id": "", "proxmox_verify_ssl": False,
        "has_secret": False,
        "ssh_host": "", "ssh_port": "", "ssh_username": "", "ssh_private_key_path": "", "ssh_command": "",
    }
    if machine is None:
        return values
    values.update(name=machine.name, mac_address=machine.mac_address, ip_address=machine.ip_address)
    shutdown = machine.shutdown
    if isinstance(shutdown, ProxmoxShutdown):
        values.update(
            shutdown_type="proxmox", proxmox_host=shutdown.host, proxmox_node=shutdown.node,
            proxmox_token_id=shutdown.token_id, proxmox_verify_ssl=shutdown.verify_ssl, has_secret=True,
        )
    elif isinstance(shutdown, SshShutdown):
        values.update(
            shutdown_type="ssh", ssh_host=shutdown.host, ssh_port=shutdown.port, ssh_username=shutdown.username,
            ssh_private_key_path=shutdown.private_key_path, ssh_command=shutdown.command,
        )
    return values
