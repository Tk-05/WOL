from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import yaml

VALID_DAYS = {"mon", "tue", "wed", "thu", "fri", "sat", "sun"}
VALID_ACTIONS = {"on", "off"}

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
class ProxmoxConfig:
    host: str
    node: str
    token_id: str
    token_secret: str
    verify_ssl: bool = True


@dataclass
class TargetConfig:
    mac_address: str
    ip_address: str


@dataclass
class ScheduleRule:
    name: str
    days: list[str]
    time: str
    action: str


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
    proxmox: ProxmoxConfig
    target: TargetConfig
    schedule: list[ScheduleRule]
    status_check: StatusCheckConfig
    wol: WolConfig
    notifications: NotificationConfig


def _require(data: dict, key: str, section: str):
    if key not in data:
        raise ConfigError(f"Missing key '{key}' in section '{section}'")
    return data[key]


def _parse_proxmox(data: dict) -> ProxmoxConfig:
    return ProxmoxConfig(
        host=_require(data, "host", "proxmox").rstrip("/"),
        node=_require(data, "node", "proxmox"),
        token_id=_require(data, "token_id", "proxmox"),
        token_secret=_require(data, "token_secret", "proxmox"),
        verify_ssl=data.get("verify_ssl", True),
    )


def _parse_target(data: dict) -> TargetConfig:
    return TargetConfig(
        mac_address=_require(data, "mac_address", "target"),
        ip_address=_require(data, "ip_address", "target"),
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
            )
        )
    return rules


def _bool_env(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def _config_from_env() -> dict | None:
    """Build the non-schedule parts of AppConfig from WOL_* env vars.

    Returns None if none of the required vars are set (pure file-based config).
    Raises ConfigError if some but not all required vars are set, since that's
    almost certainly a mistake rather than an intentional partial setup.
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

    return {
        "proxmox": ProxmoxConfig(
            host=os.environ["WOL_PROXMOX_HOST"].rstrip("/"),
            node=os.environ["WOL_PROXMOX_NODE"],
            token_id=os.environ["WOL_PROXMOX_TOKEN_ID"],
            token_secret=os.environ["WOL_PROXMOX_TOKEN_SECRET"],
            verify_ssl=_bool_env("WOL_PROXMOX_VERIFY_SSL", False),
        ),
        "target": TargetConfig(
            mac_address=os.environ["WOL_TARGET_MAC"],
            ip_address=os.environ["WOL_TARGET_IP"],
        ),
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


def load_config(path: str | Path) -> AppConfig:
    path = Path(path)
    env_config = _config_from_env()

    if path.exists():
        with path.open(encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        schedule = _parse_schedule(raw.get("schedule", []))
    elif env_config is None:
        raise ConfigError(
            f"Config file not found: {path} (copy config.yaml.example and adjust it, "
            "or set the WOL_* environment variables, see docs/portainer-setup.md)"
        )
    else:
        raw = {}
        schedule = []

    if env_config is not None:
        # Env vars always win for deployment settings (so a redeploy with a changed
        # token/IP takes effect); the schedule is only ever managed via the web UI,
        # so it's preserved from the existing file if there is one.
        config = AppConfig(schedule=schedule, **env_config)
        save_config(config, path)
    else:
        config = AppConfig(
            proxmox=_parse_proxmox(_require(raw, "proxmox", "root")),
            target=_parse_target(_require(raw, "target", "root")),
            schedule=schedule,
            status_check=_parse_status_check(raw.get("status_check")),
            wol=_parse_wol(raw.get("wol")),
            notifications=_parse_notifications(raw.get("notifications")),
        )

    return config


def save_config(config: AppConfig, path: str | Path) -> None:
    path = Path(path)
    if path.exists():
        _backup_config(path)

    notifications = {}
    if config.notifications.ntfy:
        notifications["ntfy"] = {"url": config.notifications.ntfy.url}
    if config.notifications.telegram:
        notifications["telegram"] = {
            "bot_token": config.notifications.telegram.bot_token,
            "chat_id": config.notifications.telegram.chat_id,
        }

    data = {
        "proxmox": {
            "host": config.proxmox.host,
            "node": config.proxmox.node,
            "token_id": config.proxmox.token_id,
            "token_secret": config.proxmox.token_secret,
            "verify_ssl": config.proxmox.verify_ssl,
        },
        "target": {
            "mac_address": config.target.mac_address,
            "ip_address": config.target.ip_address,
        },
        "status_check": {
            "timeout_seconds": config.status_check.timeout_seconds,
        },
        "wol": {
            "verify_after_seconds": config.wol.verify_after_seconds,
            "retry_interval_seconds": config.wol.retry_interval_seconds,
            "max_retries": config.wol.max_retries,
        },
        "notifications": notifications,
        "schedule": [
            {"name": rule.name, "days": rule.days, "time": rule.time, "action": rule.action}
            for rule in config.schedule
        ],
    }
    with path.open("w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)


def _backup_config(path: Path, keep: int = 5) -> None:
    backup_dir = path.parent / "backups"
    backup_dir.mkdir(exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    backup_path = backup_dir / f"{path.stem}-{timestamp}{path.suffix}"
    shutil.copy2(path, backup_path)

    existing = sorted(backup_dir.glob(f"{path.stem}-*{path.suffix}"))
    for old_backup in existing[:-keep]:
        old_backup.unlink()
