"""Настольный режим (ТЗ настольной программы, docs/TOR-desktop.md, НП-2, НП-9…НП-11).

Один человек за компьютером: экрана входа нет. Программа при запуске создаёт
одноразовый ключ, окно открывается на /desktop/enter?key=…, сервер сверяет ключ,
сразу его гасит и ставит обычную сессию единственной учётной записи-администратора.
Код прав не отключается: администратор проходит все проверки как раньше.
"""
from __future__ import annotations

import hashlib
import secrets
import threading

from .auth import generate_password, hash_password
from .db import Database

LOCAL_LOGIN = "local"
LOCAL_NAME = "Локальный пользователь"
KEY_BYTES = 32


def ensure_local_admin(db: Database):
    """Учётная запись программы: создаётся при первом запуске. Пароль случайный,
    нигде не показывается и не хранится — войти по паролю нельзя."""
    row = db.query_one("SELECT * FROM users WHERE login = ?", (LOCAL_LOGIN,))
    if row is None:
        db.insert(
            "INSERT INTO users (login, password_hash, role, name) VALUES (?, ?, 'admin', ?)",
            (LOCAL_LOGIN, hash_password(generate_password()), LOCAL_NAME),
        )
    else:
        # Учётную запись могли поправить в базе руками: программа без неё не откроется
        db.execute(
            "UPDATE users SET role = 'admin', status = 'active', expires_at = NULL WHERE id = ?",
            (row["id"],),
        )
    # Сессии прошлых запусков больше не действуют (НП-11)
    db.execute("UPDATE users SET session_epoch = session_epoch + 1 WHERE login = ?", (LOCAL_LOGIN,))
    return db.query_one("SELECT * FROM users WHERE login = ?", (LOCAL_LOGIN,))


class LaunchKeys:
    """Одноразовые ключи входа. Хранится только хэш: ключ живёт в памяти процесса
    программы и в адресе, на который она открывает окно."""

    def __init__(self) -> None:
        self._hashes: set[str] = set()
        self._lock = threading.Lock()

    @staticmethod
    def _digest(key: str) -> str:
        return hashlib.sha256(key.encode("utf-8")).hexdigest()

    def issue(self) -> str:
        key = secrets.token_urlsafe(KEY_BYTES)
        with self._lock:
            self._hashes.add(self._digest(key))
        return key

    def consume(self, key: str | None) -> bool:
        """Верный ключ гасится сразу: второй заход по тому же адресу получает отказ."""
        if not key:
            return False
        digest = self._digest(key)
        with self._lock:
            for known in self._hashes:
                if secrets.compare_digest(known, digest):
                    self._hashes.discard(known)
                    return True
        return False
