from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from threading import Lock

import yaml
from werkzeug.security import check_password_hash, generate_password_hash

MIN_PASSWORD_LENGTH = 8
MAX_API_KEY_NAME_LENGTH = 40
# read: only the GET status endpoints. control: also wake, shut down and reboot.
API_KEY_SCOPES = ("read", "control")


@dataclass(frozen=True)
class ApiKey:
    id: str
    name: str
    scope: str
    created: str | None

    @property
    def can_control(self) -> bool:
        return self.scope == "control"


class AuthStore:
    """Login password, session signing secret and API keys, kept in their own file next to
    config.yaml (auth.yaml) so config import/export never touches them. Passwords and API
    keys are only stored as hashes. Deleting the file and restarting resets the login."""

    def __init__(self, path: Path):
        self._path = Path(path)
        self._lock = Lock()
        self._data: dict = {}
        if self._path.exists():
            self._data = yaml.safe_load(self._path.read_text(encoding="utf-8")) or {}
        self._migrate_legacy_api_key()

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        text = yaml.safe_dump(self._data, sort_keys=False)
        fd = os.open(self._path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)

    def session_secret(self) -> str:
        with self._lock:
            if not self._data.get("session_secret"):
                self._data["session_secret"] = secrets.token_hex(32)
                self._save()
            return self._data["session_secret"]

    @property
    def has_password(self) -> bool:
        return bool(self._data.get("password_hash"))

    def check_password(self, password: str) -> bool:
        stored = self._data.get("password_hash")
        return bool(stored) and check_password_hash(stored, password)

    def set_password(self, password: str) -> None:
        if len(password) < MIN_PASSWORD_LENGTH:
            raise ValueError(f"Password must be at least {MIN_PASSWORD_LENGTH} characters")
        with self._lock:
            self._data["password_hash"] = generate_password_hash(password)
            self._save()

    def password_fingerprint(self) -> str:
        """Changes whenever the password does; stored in sessions so a password change logs
        out every other browser."""
        stored = self._data.get("password_hash", "")
        return hashlib.sha256(stored.encode()).hexdigest()[:16]

    def _migrate_legacy_api_key(self) -> None:
        """Before named keys there was one key, which could only read; it keeps doing that."""
        legacy_hash = self._data.pop("api_key_hash", None)
        legacy_created = self._data.pop("api_key_created", None)
        if legacy_hash is None:
            return
        self._data.setdefault("api_keys", []).append({
            "id": secrets.token_hex(4),
            "name": "API key",
            "scope": "read",
            "hash": legacy_hash,
            "created": legacy_created,
        })
        self._save()

    @property
    def api_keys(self) -> list[ApiKey]:
        return [_api_key_from_dict(entry) for entry in self._data.get("api_keys", [])]

    def create_api_key(self, name: str, scope: str) -> str:
        """Returns the new key once; only its hash is kept, so it can't be shown again later."""
        name = name.strip()
        if not name:
            raise ValueError("Give the key a name, e.g. the service that will use it")
        if len(name) > MAX_API_KEY_NAME_LENGTH:
            raise ValueError(f"Key names can be at most {MAX_API_KEY_NAME_LENGTH} characters")
        if scope not in API_KEY_SCOPES:
            raise ValueError(f"Invalid access level '{scope}'")
        key = secrets.token_urlsafe(32)
        with self._lock:
            entries = self._data.setdefault("api_keys", [])
            if any(entry["name"].lower() == name.lower() for entry in entries):
                raise ValueError(f"A key named '{name}' already exists")
            entries.append({
                "id": secrets.token_hex(4),
                "name": name,
                "scope": scope,
                "hash": _hash_api_key(key),
                "created": datetime.now().astimezone().isoformat(timespec="seconds"),
            })
            self._save()
        return key

    def revoke_api_key(self, key_id: str) -> ApiKey | None:
        with self._lock:
            entries = self._data.get("api_keys", [])
            for entry in entries:
                if entry["id"] == key_id:
                    entries.remove(entry)
                    self._save()
                    return _api_key_from_dict(entry)
        return None

    def check_api_key(self, key: str) -> ApiKey | None:
        if not key:
            return None
        hashed = _hash_api_key(key)
        found = None
        # No early exit, so the time taken doesn't tell which entry matched.
        for entry in list(self._data.get("api_keys", [])):
            if hmac.compare_digest(entry["hash"], hashed):
                found = entry
        return _api_key_from_dict(found) if found is not None else None


def _hash_api_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def _api_key_from_dict(entry: dict) -> ApiKey:
    return ApiKey(id=entry["id"], name=entry["name"], scope=entry["scope"], created=entry.get("created"))
