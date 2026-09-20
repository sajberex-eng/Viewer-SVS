"""Права в памяти процесса: проверка на запрос без обращения к базе (СК-3).

Каждый тайл проверяется отдельно, и раньше это стоило пяти запросов к базе —
учётная запись, дерево папок, разрешения пользователя и его групп. На экране
из тридцати готовых тайлов набегало около 170 мс на одной только проверке.

Здесь учётная запись и построенный `AccessIndex` держатся в памяти процесса.
Сброс — при любом изменении доступа, папок, групп и пользователей: его делает
`main.py` после каждого изменяющего запроса, поэтому отзыв доступа и
блокировка действуют со следующего же запроса, как и раньше. Короткий срок
годности нужен на случай правки базы со стороны (`python -m server.manage`):
такое изменение подхватывается не позже чем через TTL_SECONDS.

Сервис работает одним процессом (uvicorn без --workers), поэтому память общая
для всех запросов.
"""
from __future__ import annotations

import sqlite3
import threading
import time

from .access import AccessIndex
from .db import Database

TTL_SECONDS = 3.0


class PermissionCache:
    def __init__(self, db: Database, ttl: float = TTL_SECONDS):
        self._db = db
        self._ttl = ttl
        self._lock = threading.Lock()
        self._users: dict[int, tuple[float, sqlite3.Row | None]] = {}
        self._indexes: dict[int, tuple[float, AccessIndex]] = {}

    def invalidate(self) -> None:
        """Забыть всё: вызывается после любого изменения прав, папок и учётных записей."""
        with self._lock:
            self._users.clear()
            self._indexes.clear()

    def user(self, user_id: int) -> sqlite3.Row | None:
        """Учётная запись для проверки сессии. Отсутствие записи тоже запоминается."""
        now = time.monotonic()
        with self._lock:
            hit = self._users.get(user_id)
            if hit is not None and now - hit[0] < self._ttl:
                return hit[1]
        row = self._db.query_one("SELECT * FROM users WHERE id = ?", (user_id,))
        with self._lock:
            self._users[user_id] = (now, row)
        return row

    def index(self, user) -> AccessIndex:
        """Права одного пользователя. Построенный индекс только читается, поэтому
        его безопасно отдавать сразу нескольким потокам."""
        now = time.monotonic()
        with self._lock:
            hit = self._indexes.get(user["id"])
            if hit is not None and now - hit[0] < self._ttl:
                return hit[1]
        # Строится вне замка: чтение базы не должно задерживать другие потоки.
        # Два потока могут построить индекс одновременно — это не ошибка.
        index = AccessIndex(self._db, user)
        with self._lock:
            self._indexes[user["id"]] = (now, index)
        return index
