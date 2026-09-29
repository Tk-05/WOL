from __future__ import annotations

import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

from apscheduler.schedulers.background import BackgroundScheduler
from flask import Flask, Response, abort, flash, jsonify, redirect, render_template, request, session, url_for

from .auth import MIN_PASSWORD_LENGTH, AuthStore
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
_ACTION_MESSAGES = {
    ("on", "ok"): ("success", "Wake-up sent to '{name}'"),
    ("on", "skipped"): ("success", "'{name}' is already online"),
    ("off", "ok"): ("success", "Shutdown sent to '{name}'"),
    ("off", "skipped"): ("success", "'{name}' is already offline"),
}

logger = logging.getLogger("wol_daemon")


def _log(owner: str | None, message: str, *args) -> None:
    logger.info(message, *args, extra={"owners": (owner,) if owner else ()})


def _safe_local_path(target: str | None) -> str | None:
    """Only a path within this app; a full URL or "//host" would make redirects to it an
    open redirect."""
    if target and target.startswith("/") and not target.startswith("//") and "\\" not in target:
        return target
    return None


# Reachable without logging in. Everything under /api uses the API key instead.
_PUBLIC_ENDPOINTS = {"static", "login", "setup", "healthz"}


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
    auth = AuthStore(Path(config_path).parent / "auth.yaml")
    # Persisted, so logins survive restarts and redeploys.
    app.secret_key = auth.session_secret()
    app.config.update(
        MAX_CONTENT_LENGTH=1024 * 1024,  # config imports are tiny; refuse anything big
        SESSION_COOKIE_HTTPONLY=True,
        # Lax: the browser doesn't send the login cookie with form posts from other sites.
        SESSION_COOKIE_SAMESITE="Lax",
        PERMANENT_SESSION_LIFETIME=timedelta(days=30),
    )
    app.json.sort_keys = False  # keep the API's field order as documented in docs/api.md

    def logged_in() -> bool:
        return auth.has_password and session.get("auth") == auth.password_fingerprint()

    def log_in() -> None:
        session.clear()
        session.permanent = True
        session["auth"] = auth.password_fingerprint()

    def api_key_ok() -> bool:
        header = request.headers.get("Authorization", "")
        scheme, _, key = header.partition(" ")
        return scheme.lower() == "bearer" and auth.check_api_key(key.strip())

    def same_origin() -> bool:
        origin = request.headers.get("Origin")
        # No Origin: not a browser form post from another site (curl, scripts, old browsers).
        return origin is None or urlsplit(origin).netloc == request.host

    @app.before_request
    def require_login():
        # Second line of defence behind SameSite: refuse state-changing requests that a
        # browser sent on behalf of another site.
        if request.method not in ("GET", "HEAD", "OPTIONS") and not same_origin():
            abort(403)
        endpoint = request.endpoint
        if endpoint is None or endpoint in _PUBLIC_ENDPOINTS:
            return None
        if request.path.startswith("/api/"):
            if logged_in() or api_key_ok():
                return None
            return api_response(
                {"error": "API key required: create one under Settings and send it as 'Authorization: Bearer <key>'"},
                401,
            )
        if not auth.has_password:
            return redirect(url_for("setup"))
        if not logged_in():
            wanted = request.full_path.rstrip("?") if request.method == "GET" else None
            return redirect(url_for("login", next=wanted) if wanted and wanted != "/" else url_for("login"))
        return None

    @app.context_processor
    def template_globals():
        return {"timezone": str(scheduler.timezone), "logged_in": logged_in()}

    @app.route("/healthz")
    def healthz():
        return "ok"

    @app.route("/setup", methods=["GET", "POST"])
    def setup():
        if auth.has_password:
            return redirect(url_for("login"))
        if request.method == "GET":
            return render_template("setup.html", min_length=MIN_PASSWORD_LENGTH)
        password = request.form.get("password", "")
        if password != request.form.get("confirm", ""):
            flash("The two passwords don't match", "error")
            return redirect(url_for("setup"))
        try:
            auth.set_password(password)
        except ValueError as exc:
            flash(str(exc), "error")
            return redirect(url_for("setup"))
        log_in()
        _log(None, "Login password set")
        flash("Password set, you're logged in", "success")
        return redirect(url_for("index"))

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if not auth.has_password:
            return redirect(url_for("setup"))
        target = _safe_local_path(request.values.get("next")) or url_for("index")
        if request.method == "GET":
            if logged_in():
                return redirect(target)
            return render_template("login.html", next=request.args.get("next", ""))
        if not auth.check_password(request.form.get("password", "")):
            time.sleep(1)  # slows down guessing
            flash("Wrong password", "error")
            return redirect(url_for("login", next=_safe_local_path(request.form.get("next"))))
        log_in()
        return redirect(target)

    @app.post("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login"))

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

    def next_run(prefix: str, schedule: list[ScheduleRule]):
        """The owner's soonest scheduled job, its rule (if still present) and whether it's skipped."""
        jobs = [
            j for j in scheduler.get_jobs()
            if j.id.startswith(prefix) and getattr(j, "next_run_time", None) is not None
        ]
        if not jobs:
            return None
        job = min(jobs, key=lambda j: j.next_run_time)
        index = int(job.id[len(prefix):])
        rule = schedule[index] if 0 <= index < len(schedule) else None
        will_skip = rule is not None and rule.skip_date == job.next_run_time.date().isoformat()
        return job, rule, will_skip

    def next_action_info(prefix: str, schedule: list[ScheduleRule]) -> str | None:
        found = next_run(prefix, schedule)
        if found is None:
            return None
        job, _, will_skip = found
        text = f"{job.name} — {job.next_run_time.strftime('%a %d %b %H:%M')}"
        return text + " (will be skipped)" if will_skip else text

    def next_action_json(prefix: str, schedule: list[ScheduleRule]) -> dict | None:
        found = next_run(prefix, schedule)
        if found is None:
            return None
        job, rule, will_skip = found
        return {
            "rule": job.name,
            "action": rule.action if rule else None,
            "time": job.next_run_time.isoformat(timespec="seconds"),
            "will_be_skipped": will_skip,
        }

    def last_action_json(event_key: str) -> dict | None:
        record = event_log.last_action_for(event_key)
        if record is None:
            return None
        return {
            "time": record.timestamp.astimezone().isoformat(timespec="seconds"),
            "action": record.action,
            "source": record.source,
            "result": record.result,
            "detail": record.detail,
        }

    def machine_json(machine: Machine, online: bool) -> dict:
        return {
            "key": machine.key,
            "name": machine.name,
            "online": online,
            "ip_address": machine.ip_address,
            "mac_address": machine.mac_address,
            "shutdown_method": _shutdown_method(machine),
            "clusters": [c.key for c in config.clusters if machine.key in c.members],
            "last_action": last_action_json(machine.key),
            "next_action": next_action_json(machine_job_prefix(machine), machine.schedule),
        }

    def cluster_json(cluster: Cluster, status: dict[str, bool]) -> dict:
        members = [m for m in (get_machine(key) for key in cluster.members) if m is not None]
        online = sum(1 for m in members if status.get(m.key))
        if members and online == len(members):
            state = "on"
        elif online == 0:
            state = "off"
        else:
            state = "partial"
        return {
            "key": cluster.key,
            "name": cluster.name,
            "state": state,
            "online": online,
            "total": len(members),
            "delay_seconds": cluster.delay_seconds,
            "max_wait_seconds": cluster.max_wait_seconds,
            "members": [{"key": m.key, "name": m.name, "online": status.get(m.key, False)} for m in members],
            "last_action": last_action_json(owner_event_key(cluster)),
            "next_action": next_action_json(cluster_job_prefix(cluster), cluster.schedule),
        }

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
            "max_wait_seconds": cluster.max_wait_seconds,
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
        _log(owner_event_key(owner), "'%s': rule '%s' added (%s)", owner.name, rule.name, _rule_summary(rule))
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
        _log(owner_event_key(owner), "'%s': rule '%s' changed (%s)", owner.name, rule.name, _rule_summary(rule))
        flash("Rule saved", "success")
        return redirect(back)

    def delete_rule(owner: Machine | Cluster, index: int, back: str):
        if 0 <= index < len(owner.schedule):
            rule = owner.schedule.pop(index)
            save_config(config, config_path)
            rebuild_jobs()
            _log(owner_event_key(owner), "'%s': rule '%s' deleted", owner.name, rule.name)
            flash("Rule deleted", "success")
        return redirect(back)

    def skip_rule(owner: Machine | Cluster, index: int, job_id: str, back: str):
        if not 0 <= index < len(owner.schedule):
            flash("Rule not found", "error")
            return redirect(back)
        rule = owner.schedule[index]
        if rule.skip_date is not None:
            rule.skip_date = None
            _log(owner_event_key(owner), "'%s': skip of rule '%s' cancelled", owner.name, rule.name)
            flash("Skip cancelled", "success")
        else:
            job = scheduler.get_job(job_id)
            if job is None or job.next_run_time is None:
                flash("No upcoming run to skip", "error")
                return redirect(back)
            rule.skip_date = job.next_run_time.date().isoformat()
            _log(owner_event_key(owner), "'%s': rule '%s' will be skipped on %s", owner.name, rule.name, rule.skip_date)
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

    def redirect_back(default: str):
        """Return to the page a form was submitted from (e.g. the overview), if it said so."""
        return redirect(_safe_local_path(request.form.get("next")) or default)

    def flash_action_result(machine: Machine) -> None:
        record = event_log.last_action_for(machine.key)
        if record is None:
            return
        if record.result == "error":
            flash(f"'{machine.name}': {record.detail}", "error")
            return
        category, template = _ACTION_MESSAGES.get((record.action, record.result), ("success", "Done"))
        flash(template.format(name=machine.name), category)

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
        _log(machine.key, "Machine '%s' added", machine.name)
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
                _log(owner_event_key(cluster), "'%s' removed from cluster '%s' (machine deleted)", machine.name, cluster.name)
        save_config(config, config_path)
        rebuild_jobs()
        event_log.forget(key)
        _log(None, "Machine '%s' deleted", machine.name)
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
            owner_events=event_log.recent_events_for(machine.key),
            token_url=url_for("machine_token", key=key) if isinstance(machine.shutdown, ProxmoxShutdown) else None,
        )

    @app.route("/machines/<key>/token")
    def machine_token(key: str):
        """The Proxmox token secret is only ever sent on explicit request ('Show' button),
        never embedded in a page."""
        machine = get_machine(key)
        if machine is None or not isinstance(machine.shutdown, ProxmoxShutdown):
            abort(404)
        response = jsonify(token_id=machine.shutdown.token_id, token_secret=machine.shutdown.token_secret)
        response.headers["Cache-Control"] = "no-store"
        return response

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
        changed = [
            label for label, old, new in (
                ("name", machine.name, name),
                ("MAC address", machine.mac_address, mac_address),
                ("IP address", machine.ip_address, ip_address),
                ("shutdown settings", machine.shutdown, shutdown),
            )
            if old != new
        ]
        # Update the existing object instead of replacing it: scheduled rule jobs, pending wake
        # checks and running cluster steps all hold a reference to it, and must see the change.
        machine.name = name
        machine.mac_address = mac_address
        machine.ip_address = ip_address
        machine.shutdown = shutdown
        save_config(config, config_path)
        if changed:
            _log(machine.key, "'%s': settings changed (%s)", machine.name, ", ".join(changed))
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
        flash_action_result(machine)
        return redirect_back(url_for("machine_detail", key=key))

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
                "add_cluster.html", machines=machine_choices(), cluster_name="",
                delay_seconds=60, max_wait_seconds=300, positions={},
            )
        try:
            key = request.form.get("key", "").strip().lower()
            if not key or not KEY_RE.match(key):
                raise ConfigError("Key must use lowercase letters, digits and hyphens only, e.g. 'homelab'")
            if get_cluster(key) is not None:
                raise ConfigError(f"A cluster with key '{key}' already exists")
            members, delay, max_wait = _cluster_members_from_form(request.form, config.machines)
        except ConfigError as exc:
            flash(str(exc), "error")
            return redirect(url_for("cluster_add"))
        name = request.form.get("name", "").strip() or key
        cluster = Cluster(
            key=key, name=name, members=members, delay_seconds=delay, schedule=[], max_wait_seconds=max_wait
        )
        config.clusters.append(cluster)
        save_config(config, config_path)
        _log(owner_event_key(cluster), "Cluster '%s' added (%s)", cluster.name, _cluster_summary_text(cluster, config))
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
            max_wait_seconds=cluster.max_wait_seconds,
            owner_key=cluster.key,
            sched_ep="cluster_schedule",
            owner_events=event_log.recent_events_for(owner_event_key(cluster)),
        )

    @app.post("/clusters/<key>/edit")
    def cluster_edit(key: str):
        cluster = cluster_or_redirect(key)
        if cluster is None:
            return redirect(url_for("index"))
        try:
            members, delay, max_wait = _cluster_members_from_form(request.form, config.machines)
        except ConfigError as exc:
            flash(str(exc), "error")
            return redirect(url_for("cluster_detail", key=key))
        before = (cluster.name, list(cluster.members), cluster.delay_seconds, cluster.max_wait_seconds)
        cluster.name = request.form.get("name", "").strip() or cluster.name
        cluster.members = members
        cluster.delay_seconds = delay
        cluster.max_wait_seconds = max_wait
        save_config(config, config_path)
        if (cluster.name, cluster.members, cluster.delay_seconds, cluster.max_wait_seconds) != before:
            _log(owner_event_key(cluster), "Cluster '%s' changed (%s)", cluster.name, _cluster_summary_text(cluster, config))
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
        event_log.forget(owner_event_key(cluster))
        _log(None, "Cluster '%s' deleted", cluster.name)
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
            flash(f"{verb} {len(cluster.members)} machine(s) one after another — see the event log", "success")
        return redirect_back(url_for("cluster_detail", key=key))

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

    def render_settings(new_api_key: str | None = None):
        return render_template(
            "config.html",
            env_active=env_config_active(),
            min_length=MIN_PASSWORD_LENGTH,
            has_api_key=auth.has_api_key,
            api_key_created=auth.api_key_created,
            new_api_key=new_api_key,
        )

    @app.route("/config")
    def config_page():
        return render_settings()

    @app.post("/settings/password")
    def settings_password():
        if not auth.check_password(request.form.get("current", "")):
            time.sleep(1)
            flash("Current password is wrong", "error")
            return redirect(url_for("config_page"))
        password = request.form.get("password", "")
        if password != request.form.get("confirm", ""):
            flash("The two new passwords don't match", "error")
            return redirect(url_for("config_page"))
        try:
            auth.set_password(password)
        except ValueError as exc:
            flash(str(exc), "error")
            return redirect(url_for("config_page"))
        log_in()  # this browser stays logged in; every other one is logged out
        _log(None, "Login password changed")
        flash("Password changed. Other browsers have been logged out.", "success")
        return redirect(url_for("config_page"))

    @app.post("/settings/api-key")
    def settings_api_key():
        replaced = auth.has_api_key
        key = auth.create_api_key()
        _log(None, "API key %s", "regenerated" if replaced else "created")
        # Rendered directly instead of redirecting, so the key never passes through the
        # session cookie; it's shown this once only.
        return render_settings(new_api_key=key)

    @app.post("/settings/api-key/delete")
    def settings_api_key_delete():
        auth.revoke_api_key()
        _log(None, "API key revoked")
        flash("API key revoked. The status API now only answers logged-in browsers.", "success")
        return redirect(url_for("config_page"))

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
        _log(None, "Configuration imported from '%s': %d machine(s), %d cluster(s)",
             upload.filename, len(config.machines), len(config.clusters))
        flash(
            f"Imported {len(config.machines)} machine(s) and {len(config.clusters)} cluster(s). "
            "The previous config.yaml was kept in backups/.",
            "success",
        )
        return redirect(url_for("index"))

    # --- Read-only status API for other services (Home Assistant, Uptime Kuma, scripts) ---

    def api_response(payload: dict, status: int = 200) -> Response:
        response = jsonify(payload)
        response.status_code = status
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.route("/api/machines")
    def api_machines():
        status = ping_all(config.machines)
        return api_response({"machines": [machine_json(m, status[m.key]) for m in config.machines]})

    @app.route("/api/machines/<key>")
    def api_machine(key: str):
        machine = get_machine(key)
        if machine is None:
            return api_response({"error": f"Machine '{key}' not found"}, 404)
        return api_response(machine_json(machine, ping_all([machine])[machine.key]))

    @app.route("/api/clusters")
    def api_clusters():
        member_keys = {key for cluster in config.clusters for key in cluster.members}
        status = ping_all([m for m in config.machines if m.key in member_keys])
        return api_response({"clusters": [cluster_json(c, status) for c in config.clusters]})

    @app.route("/api/clusters/<key>")
    def api_cluster(key: str):
        cluster = get_cluster(key)
        if cluster is None:
            return api_response({"error": f"Cluster '{key}' not found"}, 404)
        status = ping_all([m for m in config.machines if m.key in cluster.members])
        return api_response(cluster_json(cluster, status))

    @app.errorhandler(413)
    def upload_too_large(_error):
        flash("Import failed, nothing was changed: the file is larger than 1 MB", "error")
        return redirect(url_for("config_page"))

    return app


def _shutdown_method(machine: Machine) -> str:
    if isinstance(machine.shutdown, ProxmoxShutdown):
        return "proxmox"
    if isinstance(machine.shutdown, SshShutdown):
        return "ssh"
    return "none"


def _rule_summary(rule: ScheduleRule) -> str:
    days = ", ".join(_DAY_LABELS[d] for d in sorted(rule.days, key=_DAY_ORDER.index))
    return f"{days} {rule.time} {rule.action}"


def _cluster_summary_text(cluster: Cluster, config: AppConfig) -> str:
    names = {m.key: m.name for m in config.machines}
    order = " → ".join(names.get(key, key) for key in cluster.members) or "no members"
    wait = f"waits up to {cluster.max_wait_seconds}s" if cluster.max_wait_seconds else "no waiting"
    return f"{order}, {wait}, {cluster.delay_seconds}s pause"


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


def _seconds_field(form, name: str, default: int, label: str) -> int:
    raw = form.get(name, "").strip()
    try:
        value = int(raw) if raw else default
    except ValueError:
        raise ConfigError(f"{label} must be a whole number of seconds")
    if value < 0:
        raise ConfigError(f"{label} can't be negative")
    return value


def _cluster_members_from_form(form, machines: list[Machine]) -> tuple[list[str], int, int]:
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

    delay = _seconds_field(form, "delay_seconds", 60, "Pause")
    max_wait = _seconds_field(form, "max_wait_seconds", 300, "Maximum wait")
    return [key for _, _, key in sorted(positioned)], delay, max_wait


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
