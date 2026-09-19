"""Пароли, сессии и ограничение перебора."""
from __future__ import annotations

import threading
import time
from collections import defaultdict, deque

import bcrypt
from fastapi import HTTPException, Request

from .db import Database

ROLES = ("resident", "teacher", "admin")
MAX_PASSWORD_BYTES = 72  # ограничение bcrypt

# Хэш случайной строки: проверка для несуществующего логина занимает то же время.
_DUMMY_HASH = bcrypt.hashpw(b"dummy-password", bcrypt.gensalt())


def hash_password(password: str) -> str:
    raw = password.encode("utf-8")
    if not 8 <= len(raw) <= MAX_PASSWORD_BYTES:
        raise ValueError("Пароль должен быть от 8 символов и не длиннее 72 байт")
    return bcrypt.hashpw(raw, bcrypt.gensalt()).decode("ascii")


def verify_password(password: str, password_hash: str | None) -> bool:
    raw = password.encode("utf-8")
    if len(raw) > MAX_PASSWORD_BYTES:
        return False
    if password_hash is None:
        bcrypt.checkpw(raw, _DUMMY_HASH)
        return False
    return bcrypt.checkpw(raw, password_hash.encode("ascii"))


class LoginThrottle:
    """Не больше max_failures неудачных попыток за window секунд на пару логин + адрес."""

    def __init__(self, max_failures: int = 5, window: int = 300):
        self.max_failures = max_failures
        self.window = window
        self._failures: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def _recent(self, key: str) -> deque[float]:
        attempts = self._failures[key]
        cutoff = time.monotonic() - self.window
        while attempts and attempts[0] < cutoff:
            attempts.popleft()
        return attempts

    def blocked(self, key: str) -> bool:
        with self._lock:
            return len(self._recent(key)) >= self.max_failures

    def record_failure(self, key: str) -> None:
        with self._lock:
            self._recent(key).append(time.monotonic())

    def reset(self, key: str) -> None:
        with self._lock:
            self._failures.pop(key, None)


def authenticate(db: Database, login: str, password: str):
    row = db.query_one("SELECT * FROM users WHERE login = ?", (login,))
    if verify_password(password, row["password_hash"] if row else None):
        return row
    return None


def session_user(request: Request):
    """Пользователь текущей сессии или None. Удалённая учётная запись сессию теряет."""
    user_id = request.session.get("user_id")
    if user_id is None:
        return None
    return request.app.state.db.query_one("SELECT id, login, role, name FROM users WHERE id = ?", (user_id,))


def require_user(request: Request):
    user = session_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail="Требуется вход в систему")
    return user


def require_admin(request: Request):
    user = require_user(request)
    if user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Недостаточно прав")
    return user
