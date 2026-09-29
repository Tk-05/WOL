from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from datetime import datetime
from pathlib import Path
from threading import Lock

import yaml
from werkzeug.security import check_password_hash, generate_password_hash

MIN_PASSWORD_LENGTH = 8


class AuthStore:
    """Login password, session signing secret and API key, kept in their own file next to
    config.yaml (auth.yaml) so config import/export never touches them. Passwords and API
    keys are only stored as hashes. Deleting the file and restarting resets the login."""

    def __init__(self, path: Path):
        self._path = Path(path)
        self._lock = Lock()
        self._data: dict = {}
        if self._path.exists():
            self._data = yaml.safe_load(self._path.read_text(encoding="utf-8")) or {}

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

    @property
    def has_api_key(self) -> bool:
        return bool(self._data.get("api_key_hash"))

    @property
    def api_key_created(self) -> str | None:
        return self._data.get("api_key_created")

    def create_api_key(self) -> str:
        """Returns the new key once; only its hash is kept, so it can't be shown again later."""
        key = secrets.token_urlsafe(32)
        with self._lock:
            self._data["api_key_hash"] = hashlib.sha256(key.encode()).hexdigest()
            self._data["api_key_created"] = datetime.now().astimezone().isoformat(timespec="seconds")
            self._save()
        return key

    def revoke_api_key(self) -> None:
        with self._lock:
            self._data.pop("api_key_hash", None)
            self._data.pop("api_key_created", None)
            self._save()

    def check_api_key(self, key: str) -> bool:
        stored = self._data.get("api_key_hash")
        if not stored or not key:
            return False
        return hmac.compare_digest(stored, hashlib.sha256(key.encode()).hexdigest())
