"""Local, dependency-free authentication for the phone-facing server.

Why this exists: before this module, ``GET /`` inlined a valid mutation token
into the page for **anyone** who could reach the port, so any device on the LAN
could approve patches and run sandboxed commands.  The token only ever protected
against cross-origin CSRF — it was never an identity check.

Design constraints (the project is stdlib-only on purpose):

* Passwords are hashed with :func:`hashlib.pbkdf2_hmac` (SHA-256) plus a
  per-user random salt.  Plaintext never touches disk or logs.
* Comparisons use :func:`hmac.compare_digest`, so verification does not leak
  how many leading bytes matched.
* Sessions are opaque ``secrets.token_urlsafe`` tokens held server-side; there
  is no self-describing JWT to forge or trust on the client.
* Files are written ``0600`` and the whole store is guarded by a lock, because
  the HTTP server is threaded.

The first account created is the ``owner``.  While the store is empty the setup
endpoint is reachable exactly once; after that only ``ALLOW_SIGNUP`` (off by
default) can create more accounts, so a LAN-exposed server cannot be enrolled
into by strangers.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

PBKDF2_ROUNDS = 210_000
MIN_PASSWORD_LENGTH = 8
MAX_PASSWORD_LENGTH = 512
USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{2,31}$")
SESSION_TTL_HOURS = 12


class AuthError(Exception):
    """Raised for validation failures that are safe to show to the user."""


def hash_password(password: str, salt: bytes, rounds: int = PBKDF2_ROUNDS) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, rounds).hex()


def validate_password(password: str) -> str:
    """Return a normalised password or raise :class:`AuthError`."""
    if not password or len(password) < MIN_PASSWORD_LENGTH:
        raise AuthError(f"use at least {MIN_PASSWORD_LENGTH} characters")
    if len(password) > MAX_PASSWORD_LENGTH:
        raise AuthError("that password is too long")
    if len(set(password)) < 3:
        raise AuthError("use at least 3 different characters")
    return password


def normalise_username(username: str) -> str:
    name = (username or "").strip().lower()
    if not USERNAME_RE.match(name):
        raise AuthError("use 3-32 chars: letters, numbers, dot, dash or underscore")
    return name


@dataclass
class User:
    id: str
    username: str
    display_name: str
    salt: str
    password_hash: str
    rounds: int
    role: str
    created_at: str
    last_login: str = ""

    def public(self) -> dict[str, Any]:
        """Everything that is safe to send to a browser."""
        return {
            "id": self.id,
            "username": self.username,
            "display_name": self.display_name,
            "role": self.role,
            "created_at": self.created_at,
            "last_login": self.last_login,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self.public(), "salt": self.salt, "password_hash": self.password_hash, "rounds": self.rounds}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "User":
        return cls(
            id=str(data.get("id", "")),
            username=str(data.get("username", "")),
            display_name=str(data.get("display_name") or data.get("username", "")),
            salt=str(data.get("salt", "")),
            password_hash=str(data.get("password_hash", "")),
            rounds=int(data.get("rounds") or PBKDF2_ROUNDS),
            role=str(data.get("role", "member")),
            created_at=str(data.get("created_at", "")),
            last_login=str(data.get("last_login", "")),
        )


@dataclass
class AuthStore:
    """User + session store backed by two small JSON files."""

    directory: Path
    allow_signup: bool = False
    session_hours: int = SESSION_TTL_HOURS
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)
    _users: dict[str, User] = field(default_factory=dict, repr=False)
    _sessions: dict[str, dict[str, Any]] = field(default_factory=dict, repr=False)
    _loaded: bool = field(default=False, repr=False)

    # -- persistence ---------------------------------------------------
    @property
    def users_path(self) -> Path:
        return self.directory / "users.json"

    @property
    def sessions_path(self) -> Path:
        return self.directory / "sessions.json"

    def _ensure_dir(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        try:
            self.directory.chmod(0o700)
        except OSError:
            pass

    @staticmethod
    def _write(path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, indent=1, sort_keys=True), encoding="utf-8")
        try:
            tmp.chmod(0o600)
        except OSError:
            pass
        tmp.replace(path)

    @staticmethod
    def _read(path: Path) -> dict[str, Any]:
        if not path.is_file():
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def _load(self) -> None:
        if self._loaded:
            return
        with self._lock:
            if self._loaded:
                return
            raw_users = self._read(self.users_path).get("users", {})
            self._users = {key: User.from_dict(value) for key, value in raw_users.items()}
            self._sessions = self._read(self.sessions_path).get("sessions", {})
            self._prune_expired()
            self._loaded = True

    def _save_users(self) -> None:
        self._write(self.users_path, {"users": {k: v.to_dict() for k, v in self._users.items()}})

    def _save_sessions(self) -> None:
        self._write(self.sessions_path, {"sessions": self._sessions})

    def _prune_expired(self) -> None:
        now = time.time()
        stale = [token for token, data in self._sessions.items() if float(data.get("expires_at", 0)) <= now]
        for token in stale:
            self._sessions.pop(token, None)
        if stale:
            self._save_sessions()

    # -- accounts ------------------------------------------------------
    def needs_setup(self) -> bool:
        self._load()
        with self._lock:
            return not self._users

    def list_users(self) -> list[dict[str, Any]]:
        self._load()
        with self._lock:
            return [user.public() for user in sorted(self._users.values(), key=lambda u: u.created_at)]

    def get_user(self, username: str) -> User | None:
        self._load()
        with self._lock:
            return self._users.get(normalise_username(username))

    def create_user(self, username: str, password: str, *, role: str = "member", display_name: str = "") -> User:
        self._load()
        name = normalise_username(username)
        secret = validate_password(password)
        with self._lock:
            if name in self._users:
                raise AuthError("that username is already taken")
            salt = secrets.token_bytes(16)
            user = User(
                id=f"usr_{secrets.token_hex(6)}",
                username=name,
                display_name=(display_name or name).strip()[:64] or name,
                salt=salt.hex(),
                password_hash=hash_password(secret, salt),
                rounds=PBKDF2_ROUNDS,
                role="owner" if not self._users else role,
                created_at=_now_iso(),
            )
            self._users[name] = user
            self._save_users()
            return user

    def authenticate(self, username: str, password: str) -> User | None:
        self._load()
        try:
            name = normalise_username(username)
        except AuthError:
            return None
        with self._lock:
            user = self._users.get(name)
        if user is None:
            # Hash a dummy value so a missing user costs the same time as a
            # wrong password, keeping the endpoint from being a user oracle.
            hash_password(password or "x", b"fixpilot-timing-equaliser")
            return None
        candidate = hash_password(password or "", bytes.fromhex(user.salt), user.rounds)
        if not hmac.compare_digest(candidate, user.password_hash):
            return None
        with self._lock:
            user.last_login = _now_iso()
            self._save_users()
        return user

    def set_password(self, username: str, password: str) -> bool:
        self._load()
        name = normalise_username(username)
        secret = validate_password(password)
        with self._lock:
            user = self._users.get(name)
            if user is None:
                return False
            salt = secrets.token_bytes(16)
            user.salt = salt.hex()
            user.rounds = PBKDF2_ROUNDS
            user.password_hash = hash_password(secret, salt)
            self._save_users()
            self.revoke_all_for(user.id)
            return True

    # -- sessions ------------------------------------------------------
    def create_session(self, user: User, *, user_agent: str = "", remote: str = "") -> str:
        self._load()
        token = secrets.token_urlsafe(32)
        expires = time.time() + max(1, self.session_hours) * 3600
        with self._lock:
            self._sessions[token] = {
                "user": user.username,
                "created_at": _now_iso(),
                "expires_at": expires,
                "user_agent": user_agent[:200],
                "remote": remote[:64],
            }
            self._save_sessions()
        return token

    def session_user(self, token: str) -> User | None:
        if not token:
            return None
        self._load()
        with self._lock:
            data = self._sessions.get(token)
            if data is None:
                return None
            if float(data.get("expires_at", 0)) <= time.time():
                self._sessions.pop(token, None)
                self._save_sessions()
                return None
            return self._users.get(str(data.get("user", "")))

    def revoke(self, token: str) -> bool:
        self._load()
        with self._lock:
            if self._sessions.pop(token, None) is None:
                return False
            self._save_sessions()
            return True

    def revoke_all_for(self, user_id: str) -> int:
        self._load()
        with self._lock:
            username = next((u.username for u in self._users.values() if u.id == user_id), "")
            doomed = [t for t, d in self._sessions.items() if d.get("user") == username]
            for token in doomed:
                self._sessions.pop(token, None)
            if doomed:
                self._save_sessions()
            return len(doomed)

    def active_sessions(self) -> int:
        self._load()
        with self._lock:
            self._prune_expired()
            return len(self._sessions)

    # -- description ---------------------------------------------------
    def describe(self) -> dict[str, Any]:
        """Auth state that is safe to expose *before* login (drives the UI)."""
        self._load()
        with self._lock:
            needs_setup = not self._users
        return {
            "enabled": True,
            "needs_setup": needs_setup,
            "allow_signup": bool(self.allow_signup) and not needs_setup,
            "user_count": len(self._users),
            "min_password_length": MIN_PASSWORD_LENGTH,
        }


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
