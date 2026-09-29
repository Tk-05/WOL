from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import yaml

VALID_DAYS = {"mon", "tue", "wed", "thu", "fri", "sat", "sun"}
VALID_ACTIONS = {"on", "off"}
VALID_SHUTDOWN_TYPES = {"proxmox", "ssh", "none"}
KEY_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
# Key of the one machine that WOL_* env vars define (and that legacy configs migrate to).
ENV_MACHINE_KEY = "default"

_REQUIRED_ENV_VARS = [
    "WOL_PROXMOX_HOST",
    "WOL_PROXMOX_NODE",
    "WOL_PROXMOX_TOKEN_ID",
    "WOL_PROXMOX_TOKEN_SECRET",
    "WOL_TARGET_MAC",
    "WOL_TARGET_IP",
]


class ConfigError(Exception):
    pass


@dataclass
class ProxmoxShutdown:
    host: str
    node: str
    token_id: str
    token_secret: str
    verify_ssl: bool = True


@dataclass
class SshShutdown:
    host: str
    port: int = 22
    username: str = "root"
    private_key_path: str = ""
    # A short delay (rather than an immediate shutdown) lets the ssh command return with a
    # clean exit status before the connection drops, instead of racing the shutdown itself.
    command: str = "shutdown -h +1"
    reboot_command: str = "shutdown -r +1"


@dataclass
class ScheduleRule:
    name: str
    days: list[str]
    time: str
    action: str
    skip_date: str | None = None  # ISO date; the rule's next occurrence on this date is skipped once


@dataclass
class Machine:
    key: str
    name: str
    mac_address: str
    ip_address: str
    shutdown: ProxmoxShutdown | SshShutdown | None
    schedule: list[ScheduleRule]


@dataclass
class Cluster:
    key: str
    name: str
    members: list[str]  # machine keys in wake order; shut down in reverse
    delay_seconds: int  # extra pause after each machine is up (or down), before the next one
    schedule: list[ScheduleRule]
    # How long to wait for each machine to come up (or go down) before continuing anyway;
    # 0 = don't wait at all, just pause delay_seconds between machines.
    max_wait_seconds: int = 300


@dataclass
class StatusCheckConfig:
    timeout_seconds: int = 5


@dataclass
class WolConfig:
    verify_after_seconds: int = 120
    retry_interval_seconds: int = 60
    max_retries: int = 2


@dataclass
class NtfyConfig:
    url: str


@dataclass
class TelegramConfig:
    bot_token: str
    chat_id: str


@dataclass
class NotificationConfig:
    ntfy: NtfyConfig | None = None
    telegram: TelegramConfig | None = None


@dataclass
class AppConfig:
    machines: list[Machine]
    status_check: StatusCheckConfig
    wol: WolConfig
    notifications: NotificationConfig
    clusters: list[Cluster] = field(default_factory=list)


def _require(data: dict, key: str, section: str):
    if key not in data:
        raise ConfigError(f"Missing key '{key}' in section '{section}'")
    return data[key]


def _parse_shutdown(data: dict | None, section: str) -> ProxmoxShutdown | SshShutdown | None:
    if not data:
        return None
    shutdown_type = data.get("type", "none")
    if shutdown_type not in VALID_SHUTDOWN_TYPES:
        raise ConfigError(f"Invalid shutdown type '{shutdown_type}' in {section}, allowed: {sorted(VALID_SHUTDOWN_TYPES)}")
    if shutdown_type == "none":
        return None
    if shutdown_type == "proxmox":
        return ProxmoxShutdown(
            host=_require(data, "host", section).rstrip("/"),
            node=_require(data, "node", section),
            token_id=_require(data, "token_id", section),
            token_secret=_require(data, "token_secret", section),
            verify_ssl=data.get("verify_ssl", True),
        )
    return SshShutdown(
        host=_require(data, "host", section),
        port=data.get("port", 22),
        username=data.get("username", "root"),
        private_key_path=data.get("private_key_path", ""),
        command=data.get("command", "shutdown -h +1"),
        reboot_command=data.get("reboot_command", "shutdown -r +1"),
    )


def _parse_status_check(data: dict | None) -> StatusCheckConfig:
    data = data or {}
    return StatusCheckConfig(timeout_seconds=data.get("timeout_seconds", 5))


def _parse_wol(data: dict | None) -> WolConfig:
    data = data or {}
    return WolConfig(
        verify_after_seconds=data.get("verify_after_seconds", 120),
        retry_interval_seconds=data.get("retry_interval_seconds", 60),
        max_retries=data.get("max_retries", 2),
    )


def _parse_notifications(data: dict | None) -> NotificationConfig:
    data = data or {}
    ntfy_data = data.get("ntfy")
    telegram_data = data.get("telegram")
    ntfy = NtfyConfig(url=_require(ntfy_data, "url", "notifications.ntfy")) if ntfy_data else None
    telegram = (
        TelegramConfig(
            bot_token=_require(telegram_data, "bot_token", "notifications.telegram"),
            chat_id=_require(telegram_data, "chat_id", "notifications.telegram"),
        )
        if telegram_data
        else None
    )
    return NotificationConfig(ntfy=ntfy, telegram=telegram)


def _parse_schedule(entries: list) -> list[ScheduleRule]:
    rules = []
    for i, entry in enumerate(entries):
        section = f"schedule[{i}]"
        days = _require(entry, "days", section)
        invalid_days = set(days) - VALID_DAYS
        if invalid_days:
            raise ConfigError(f"Invalid days {invalid_days} in {section}, allowed: {sorted(VALID_DAYS)}")
        action = _require(entry, "action", section)
        if action not in VALID_ACTIONS:
            raise ConfigError(f"Invalid action '{action}' in {section}, allowed: {sorted(VALID_ACTIONS)}")
        rules.append(
            ScheduleRule(
                name=entry.get("name", section),
                days=list(days),
                time=_require(entry, "time", section),
                action=action,
                skip_date=entry.get("skip_date"),
            )
        )
    return rules


def _parse_machines(entries: list) -> list[Machine]:
    machines = []
    seen_keys: set[str] = set()
    for i, entry in enumerate(entries):
        section = f"machines[{i}]"
        key = entry.get("key") or f"machine-{i}"
        if not KEY_RE.match(key):
            raise ConfigError(f"Invalid key '{key}' in {section}: use lowercase letters, digits and hyphens only")
        if key in seen_keys:
            raise ConfigError(f"Duplicate machine key '{key}'")
        seen_keys.add(key)
        machines.append(
            Machine(
                key=key,
                name=entry.get("name", key),
                mac_address=_require(entry, "mac_address", section),
                ip_address=_require(entry, "ip_address", section),
                shutdown=_parse_shutdown(entry.get("shutdown"), f"{section}.shutdown"),
                schedule=_parse_schedule(entry.get("schedule", [])),
            )
        )
    return machines


def _parse_clusters(entries: list, machine_keys: set[str]) -> list[Cluster]:
    clusters = []
    seen_keys: set[str] = set()
    for i, entry in enumerate(entries):
        section = f"clusters[{i}]"
        key = _require(entry, "key", section)
        if not isinstance(key, str) or not KEY_RE.match(key):
            raise ConfigError(f"Invalid key '{key}' in {section}: use lowercase letters, digits and hyphens only")
        if key in seen_keys:
            raise ConfigError(f"Duplicate cluster key '{key}'")
        seen_keys.add(key)
        members = list(entry.get("members") or [])
        unknown = [m for m in members if m not in machine_keys]
        if unknown:
            raise ConfigError(f"Unknown machine(s) {unknown} in {section}.members")
        if len(set(members)) != len(members):
            raise ConfigError(f"A machine is listed more than once in {section}.members")
        delay = entry.get("delay_seconds", 60)
        max_wait = entry.get("max_wait_seconds", 300)
        for field_name, value in (("delay_seconds", delay), ("max_wait_seconds", max_wait)):
            if not isinstance(value, int) or value < 0:
                raise ConfigError(f"{field_name} in {section} must be a whole number of seconds, 0 or more")
        clusters.append(
            Cluster(
                key=key,
                name=entry.get("name", key),
                members=members,
                delay_seconds=delay,
                schedule=_parse_schedule(entry.get("schedule", [])),
                max_wait_seconds=max_wait,
            )
        )
    return clusters


def _migrate_legacy(raw: dict) -> dict:
    """Convert a pre-multi-machine config.yaml (top-level proxmox/target/schedule) into the
    current machines-list shape. Runs transparently on load; the file is rewritten to the new
    shape the next time save_config() is called (e.g. via any schedule edit in the web UI)."""
    proxmox = raw.get("proxmox") or {}
    target = raw["target"]
    machine = {
        "key": ENV_MACHINE_KEY,
        "name": proxmox.get("node") or "Machine",
        "mac_address": target["mac_address"],
        "ip_address": target["ip_address"],
        "shutdown": {"type": "proxmox", **proxmox} if proxmox else None,
        "schedule": raw.get("schedule", []),
    }
    return {
        "machines": [machine],
        "status_check": raw.get("status_check"),
        "wol": raw.get("wol"),
        "notifications": raw.get("notifications"),
    }


def _bool_env(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def env_config_active() -> bool:
    return any(os.environ.get(name) for name in _REQUIRED_ENV_VARS)


def _config_from_env() -> dict | None:
    """Build a single-machine AppConfig (minus schedule) from WOL_* env vars.

    Returns None if none of the required vars are set (pure file-based config).
    Raises ConfigError if some but not all required vars are set, since that's
    almost certainly a mistake rather than an intentional partial setup.

    This only ever produces one machine (key "default") — a list of machines
    doesn't map cleanly onto a flat env var table. Further machines are added via
    the web UI and kept alongside it, see load_config().
    """
    present = [name for name in _REQUIRED_ENV_VARS if os.environ.get(name)]
    if not present:
        return None
    missing = [name for name in _REQUIRED_ENV_VARS if name not in present]
    if missing:
        raise ConfigError(f"Incomplete WOL_* environment config, missing: {', '.join(missing)}")

    ntfy_url = os.environ.get("WOL_NTFY_URL")
    telegram_token = os.environ.get("WOL_TELEGRAM_BOT_TOKEN")
    telegram_chat_id = os.environ.get("WOL_TELEGRAM_CHAT_ID")
    if bool(telegram_token) != bool(telegram_chat_id):
        raise ConfigError("WOL_TELEGRAM_BOT_TOKEN and WOL_TELEGRAM_CHAT_ID must be set together")

    machine = Machine(
        key=ENV_MACHINE_KEY,
        name=os.environ["WOL_PROXMOX_NODE"],
        mac_address=os.environ["WOL_TARGET_MAC"],
        ip_address=os.environ["WOL_TARGET_IP"],
        shutdown=ProxmoxShutdown(
            host=os.environ["WOL_PROXMOX_HOST"].rstrip("/"),
            node=os.environ["WOL_PROXMOX_NODE"],
            token_id=os.environ["WOL_PROXMOX_TOKEN_ID"],
            token_secret=os.environ["WOL_PROXMOX_TOKEN_SECRET"],
            verify_ssl=_bool_env("WOL_PROXMOX_VERIFY_SSL", False),
        ),
        schedule=[],
    )

    return {
        "machines": [machine],
        "status_check": StatusCheckConfig(
            timeout_seconds=int(os.environ.get("WOL_STATUS_TIMEOUT_SECONDS", 5)),
        ),
        "wol": WolConfig(
            verify_after_seconds=int(os.environ.get("WOL_VERIFY_AFTER_SECONDS", 120)),
            retry_interval_seconds=int(os.environ.get("WOL_RETRY_INTERVAL_SECONDS", 60)),
            max_retries=int(os.environ.get("WOL_MAX_RETRIES", 2)),
        ),
        "notifications": NotificationConfig(
            ntfy=NtfyConfig(url=ntfy_url) if ntfy_url else None,
            telegram=TelegramConfig(bot_token=telegram_token, chat_id=telegram_chat_id) if telegram_token else None,
        ),
    }


def _config_from_raw(raw: dict, env_config: dict | None) -> AppConfig:
    if "machines" not in raw and "target" in raw:
        raw = _migrate_legacy(raw)
    machines = _parse_machines(raw.get("machines") or [])

    if env_config is None:
        status_check = _parse_status_check(raw.get("status_check"))
        wol = _parse_wol(raw.get("wol"))
        notifications = _parse_notifications(raw.get("notifications"))
    else:
        # Env vars always win for their machine's deployment settings (so a redeploy with a
        # changed token/IP takes effect). Its schedule and every other machine added via the
        # web UI are kept from the existing file.
        env_machine = env_config["machines"][0]
        for i, machine in enumerate(machines):
            if machine.key == env_machine.key:
                env_machine.schedule = machine.schedule
                machines[i] = env_machine
                break
        else:
            machines.insert(0, env_machine)
        status_check = env_config["status_check"]
        wol = env_config["wol"]
        notifications = env_config["notifications"]

    return AppConfig(
        machines=machines,
        status_check=status_check,
        wol=wol,
        notifications=notifications,
        clusters=_parse_clusters(raw.get("clusters") or [], {m.key for m in machines}),
    )


def load_config(path: str | Path) -> AppConfig:
    path = Path(path)
    env_config = _config_from_env()

    # A missing file isn't an error: the daemon then starts with no machines, and adding
    # the first one in the web UI creates the file.
    raw: dict = {}
    if path.exists():
        with path.open(encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}

    config = _config_from_raw(raw, env_config)
    if env_config is not None:
        save_config(config, path)
    return config


def parse_config_text(text: str) -> AppConfig:
    """Validate an uploaded config (e.g. from the web UI's import) without touching disk.
    Environment variables are deliberately not applied here; that happens on the next load."""
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"Not valid YAML: {exc}") from exc
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ConfigError("Expected a YAML mapping (key: value pairs) at the top level")
    try:
        return _config_from_raw(raw, None)
    except (KeyError, TypeError, AttributeError, ValueError) as exc:
        raise ConfigError(f"Unexpected config structure: {exc!r}") from exc


def _shutdown_to_dict(shutdown: ProxmoxShutdown | SshShutdown | None) -> dict | None:
    if shutdown is None:
        return None
    if isinstance(shutdown, ProxmoxShutdown):
        return {
            "type": "proxmox",
            "host": shutdown.host,
            "node": shutdown.node,
            "token_id": shutdown.token_id,
            "token_secret": shutdown.token_secret,
            "verify_ssl": shutdown.verify_ssl,
        }
    return {
        "type": "ssh",
        "host": shutdown.host,
        "port": shutdown.port,
        "username": shutdown.username,
        "private_key_path": shutdown.private_key_path,
        "command": shutdown.command,
        "reboot_command": shutdown.reboot_command,
    }


def _schedule_to_list(schedule: list[ScheduleRule]) -> list[dict]:
    return [
        {
            "name": rule.name,
            "days": rule.days,
            "time": rule.time,
            "action": rule.action,
            "skip_date": rule.skip_date,
        }
        for rule in schedule
    ]


def config_to_dict(config: AppConfig) -> dict:
    notifications = {}
    if config.notifications.ntfy:
        notifications["ntfy"] = {"url": config.notifications.ntfy.url}
    if config.notifications.telegram:
        notifications["telegram"] = {
            "bot_token": config.notifications.telegram.bot_token,
            "chat_id": config.notifications.telegram.chat_id,
        }

    return {
        "machines": [
            {
                "key": machine.key,
                "name": machine.name,
                "mac_address": machine.mac_address,
                "ip_address": machine.ip_address,
                "shutdown": _shutdown_to_dict(machine.shutdown),
                "schedule": _schedule_to_list(machine.schedule),
            }
            for machine in config.machines
        ],
        "clusters": [
            {
                "key": cluster.key,
                "name": cluster.name,
                "members": cluster.members,
                "delay_seconds": cluster.delay_seconds,
                "max_wait_seconds": cluster.max_wait_seconds,
                "schedule": _schedule_to_list(cluster.schedule),
            }
            for cluster in config.clusters
        ],
        "status_check": {
            "timeout_seconds": config.status_check.timeout_seconds,
        },
        "wol": {
            "verify_after_seconds": config.wol.verify_after_seconds,
            "retry_interval_seconds": config.wol.retry_interval_seconds,
            "max_retries": config.wol.max_retries,
        },
        "notifications": notifications,
    }


def config_to_yaml(config: AppConfig) -> str:
    return yaml.safe_dump(config_to_dict(config), allow_unicode=True, sort_keys=False)


def save_config(config: AppConfig, path: str | Path) -> None:
    path = Path(path)
    if path.exists():
        _backup_config(path)
    path.write_text(config_to_yaml(config), encoding="utf-8")


def _backup_config(path: Path, keep: int = 5) -> None:
    backup_dir = path.parent / "backups"
    backup_dir.mkdir(exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    backup_path = backup_dir / f"{path.stem}-{timestamp}{path.suffix}"
    shutil.copy2(path, backup_path)

    existing = sorted(backup_dir.glob(f"{path.stem}-*{path.suffix}"))
    for old_backup in existing[:-keep]:
        old_backup.unlink()
