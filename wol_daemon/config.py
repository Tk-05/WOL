from __future__ import annotations

import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import yaml

VALID_DAYS = {"mon", "tue", "wed", "thu", "fri", "sat", "sun"}
VALID_ACTIONS = {"on", "off"}


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


def load_config(path: str | Path) -> AppConfig:
    path = Path(path)
    if not path.exists():
        raise ConfigError(
            f"Config file not found: {path} (copy config.yaml.example and adjust it)"
        )
    with path.open(encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    return AppConfig(
        proxmox=_parse_proxmox(_require(raw, "proxmox", "root")),
        target=_parse_target(_require(raw, "target", "root")),
        schedule=_parse_schedule(_require(raw, "schedule", "root")),
        status_check=_parse_status_check(raw.get("status_check")),
        wol=_parse_wol(raw.get("wol")),
        notifications=_parse_notifications(raw.get("notifications")),
    )


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
