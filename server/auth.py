"""Пароли, сессии, состояние учётной записи и ограничение перебора."""
from __future__ import annotations

import secrets
import threading
import time
from collections import defaultdict, deque
from datetime import date

import bcrypt
from fastapi import HTTPException, Request

from .db import Database

ROLES = ("user", "admin")
MAX_PASSWORD_BYTES = 72  # ограничение bcrypt
MIN_PASSWORD_LENGTH = 10  # пароль, который пользователь задаёт себе (ТЗ П-4)
GENERATED_PASSWORD_BYTES = 12  # выдаваемый администратором пароль длиннее (ТЗ П-3)

# Хэш случайной строки: проверка для несуществующего логина занимает то же время.
_DUMMY_HASH = bcrypt.hashpw(b"dummy-password", bcrypt.gensalt())


def hash_password(password: str) -> str:
    raw = password.encode("utf-8")
    if len(raw) > MAX_PASSWORD_BYTES:
        raise ValueError(f"Пароль не длиннее {MAX_PASSWORD_BYTES} байт")
    if len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"Пароль должен быть не короче {MIN_PASSWORD_LENGTH} символов")
    return bcrypt.hashpw(raw, bcrypt.gensalt()).decode("ascii")


def generate_password() -> str:
    """Случайный пароль, который администратор показывает пользователю один раз."""
    return secrets.token_urlsafe(GENERATED_PASSWORD_BYTES)


def verify_password(password: str, password_hash: str | None) -> bool:
    raw = password.encode("utf-8")
    if len(raw) > MAX_PASSWORD_BYTES:
        return False
    if password_hash is None:
        bcrypt.checkpw(raw, _DUMMY_HASH)
        return False
    return bcrypt.checkpw(raw, password_hash.encode("ascii"))


def account_problem(row) -> str | None:
    """Почему учётной записью нельзя пользоваться, либо None."""
    if row["status"] != "active":
        return "Учётная запись заблокирована. Обратитесь к администратору."
    if row["expires_at"] and date.today().isoformat() > row["expires_at"]:
        return "Срок действия учётной записи истёк. Обратитесь к администратору."
    return None


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


def start_session(request: Request, user) -> None:
    """Версия сессии запоминается: блокировка и смена пароля обрывают старые сессии."""
    request.session.clear()
    request.session["user_id"] = user["id"]
    request.session["epoch"] = user["session_epoch"]


def session_user(request: Request):
    """Пользователь текущей сессии или None.

    Отзыв доступа действует со следующего запроса: сверяем версию сессии,
    состояние и срок действия учётной записи.
    """
    user_id = request.session.get("user_id")
    if user_id is None:
        return None
    # Учётная запись берётся из памяти процесса (СК-3); блокировка, смена роли и
    # смена пароля сбрасывают эту память и потому действуют со следующего запроса.
    row = request.app.state.perms.user(user_id)
    if row is None or row["session_epoch"] != request.session.get("epoch"):
        request.session.clear()
        return None
    if account_problem(row) is not None:
        request.session.clear()
        return None
    return row


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


def set_password(db: Database, user_id: int, password: str) -> None:
    """Смена пароля обрывает все сессии этого пользователя."""
    db.execute(
        "UPDATE users SET password_hash = ?, session_epoch = session_epoch + 1 WHERE id = ?",
        (hash_password(password), user_id),
    )


def revoke_sessions(db: Database, user_id: int) -> None:
    db.execute("UPDATE users SET session_epoch = session_epoch + 1 WHERE id = ?", (user_id,))
