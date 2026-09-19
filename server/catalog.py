"""Каталог: дерево папок, карточки сканов и права на них.

Дерево живёт только в базе, файлы на диске лежат под внутренними
идентификаторами. Поэтому переименование и перемещение мгновенны и не ломают
ни ссылки на поле зрения, ни кэш тайлов.
"""
from __future__ import annotations

import logging
import re
import secrets
import sqlite3
import threading
import time

from .access import MODES, AccessIndex
from .db import Database
from .storage import Storage, StorageUnavailable
from .slides import SlidePool

log = logging.getLogger(__name__)

MAX_DEPTH = 5  # ТЗ Х-1: папки не глубже пяти уровней
MAX_NAME_LENGTH = 100  # ТЗ Х-2
STAIN_LABELS = {"HE": "H&E"}

# <код случая>_<номер стекла>_<окраска>.svs, например P004512_S03_HE.svs:
# если исходное имя такое, поля карточки заполняются сразу.
NAME_PATTERN = re.compile(r"^([A-Za-z0-9-]+)_([A-Za-z0-9-]+)_([A-Za-z0-9-]+)\.svs$", re.IGNORECASE)


class CatalogError(Exception):
    """Ошибка, понятная пользователю: показывается как есть."""


def new_slide_id() -> str:
    """Случайный идентификатор: из него получается ключ хранилища, имя файла не участвует."""
    return secrets.token_hex(6)


def parse_name(filename: str) -> tuple[str | None, str | None, str | None]:
    match = NAME_PATTERN.match(filename.rsplit("/", 1)[-1])
    return match.groups() if match else (None, None, None)


def slide_title(row) -> str:
    """Название для интерфейса. Имя файла сюда не попадает никогда."""
    if row["title"]:
        return row["title"]
    if row["case_code"]:
        stain = STAIN_LABELS.get((row["stain"] or "").upper(), row["stain"])
        return f"{row['case_code']} · {row['glass']} · {stain}"
    return f"Скан {row['id'][:6].upper()}"


def normalized(name: str) -> str:
    name = " ".join(name.split())
    if not name:
        raise CatalogError("Название не может быть пустым")
    if len(name) > MAX_NAME_LENGTH:
        raise CatalogError(f"Название не длиннее {MAX_NAME_LENGTH} символов")
    if "/" in name or "\\" in name:
        raise CatalogError("В названии нельзя использовать косую черту")
    return name


class Catalog:
    def __init__(self, db: Database, storage: Storage, pool: SlidePool, check_minutes: int):
        self._db = db
        self._storage = storage
        self._pool = pool
        self._check_interval = check_minutes * 60
        self._lock = threading.Lock()
        self._last_check = 0.0

    # ---------- чтение ----------

    def folders(self) -> list[sqlite3.Row]:
        return self._db.query("SELECT * FROM folders ORDER BY name")

    def folder(self, folder_id: int) -> sqlite3.Row | None:
        return self._db.query_one("SELECT * FROM folders WHERE id = ?", (folder_id,))

    def slides(self, folder_id: int | None = None) -> list[sqlite3.Row]:
        if folder_id is None:
            return self._db.query("SELECT * FROM slides WHERE missing = 0 ORDER BY folder_id, title, id")
        return self._db.query(
            "SELECT * FROM slides WHERE folder_id = ? AND missing = 0 ORDER BY title, id", (folder_id,)
        )

    def slide(self, slide_id: str) -> sqlite3.Row | None:
        return self._db.query_one("SELECT * FROM slides WHERE id = ? AND missing = 0", (slide_id,))

    def visible_slide(self, slide_id: str, access: AccessIndex) -> sqlite3.Row | None:
        """Скан, если он существует и доступен. Иначе None: «нет доступа» и «нет скана» неразличимы."""
        row = self.slide(slide_id)
        if row is None or not access.can_view_slide(row):
            return None
        return row

    def depth(self, folder_id: int | None) -> int:
        access = AccessIndex(self._db, None)
        return len(access.ancestors(folder_id))

    # ---------- папки ----------

    def create_folder(self, name: str, parent_id: int | None, user) -> int:
        name = normalized(name)
        if parent_id is not None:
            if self.folder(parent_id) is None:
                raise CatalogError("Родительская папка не найдена")
            if self.depth(parent_id) >= MAX_DEPTH:
                raise CatalogError(f"Глубже {MAX_DEPTH} уровней папки не создаются")
        self._check_name_free(name, parent_id)
        # Новая папка верхнего уровня закрыта для всех, кроме администраторов (ТЗ Д-1);
        # вложенная наследует режим родителя.
        mode = "admins" if parent_id is None else None
        try:
            return self._db.insert(
                "INSERT INTO folders (parent_id, name, access_mode, created_by) VALUES (?, ?, ?, ?)",
                (parent_id, name, mode, user["id"]),
            )
        except sqlite3.IntegrityError as exc:
            raise CatalogError("Папка с таким названием уже есть") from exc

    def rename_folder(self, folder_id: int, name: str) -> None:
        row = self.folder(folder_id)
        if row is None:
            raise CatalogError("Папка не найдена")
        name = normalized(name)
        self._check_name_free(name, row["parent_id"], exclude=folder_id)
        self._db.execute("UPDATE folders SET name = ? WHERE id = ?", (name, folder_id))

    def move_folder(self, folder_id: int, parent_id: int | None) -> None:
        row = self.folder(folder_id)
        if row is None:
            raise CatalogError("Папка не найдена")
        if parent_id is not None:
            if self.folder(parent_id) is None:
                raise CatalogError("Папка назначения не найдена")
            access = AccessIndex(self._db, None)
            if parent_id in access.descendants(folder_id):
                raise CatalogError("Папку нельзя переместить внутрь себя")
            if self.depth(parent_id) + self._subtree_height(folder_id) > MAX_DEPTH:
                raise CatalogError(f"После перемещения вложенность превысит {MAX_DEPTH} уровней")
        elif row["access_mode"] is None:
            # На верхнем уровне наследовать не от кого
            raise CatalogError("Сначала задайте папке свой режим доступа")
        self._check_name_free(row["name"], parent_id, exclude=folder_id)
        self._db.execute("UPDATE folders SET parent_id = ? WHERE id = ?", (parent_id, folder_id))

    def folder_contents_count(self, folder_id: int) -> tuple[int, int]:
        """Сколько подпапок и сканов внутри, на всех уровнях."""
        access = AccessIndex(self._db, None)
        ids = access.descendants(folder_id)
        placeholders = ",".join("?" * len(ids))
        slides = self._db.query_one(
            f"SELECT count(*) AS n FROM slides WHERE folder_id IN ({placeholders})", tuple(ids)
        )
        return len(ids) - 1, slides["n"]

    def delete_folder(self, folder_id: int) -> tuple[int, int]:
        """Удаляет папку со всем содержимым. Файлы сканов стираются с диска."""
        if self.folder(folder_id) is None:
            raise CatalogError("Папка не найдена")
        access = AccessIndex(self._db, None)
        ids = sorted(access.descendants(folder_id), key=lambda fid: -self.depth(fid))
        removed_slides = 0
        for fid in ids:
            for row in self._db.query("SELECT id FROM slides WHERE folder_id = ?", (fid,)):
                self.delete_slide(row["id"])
                removed_slides += 1
            self._db.execute("DELETE FROM folders WHERE id = ?", (fid,))
        return len(ids), removed_slides

    def _subtree_height(self, folder_id: int) -> int:
        access = AccessIndex(self._db, None)
        return max(self.depth(fid) for fid in access.descendants(folder_id)) - self.depth(folder_id) + 1

    def _check_name_free(self, name: str, parent_id: int | None, exclude: int | None = None) -> None:
        """Сравнение без учёта регистра: «Случай 01» и «случай 01» это одно и то же."""
        if parent_id is None:
            rows = self._db.query("SELECT id, name FROM folders WHERE parent_id IS NULL")
        else:
            rows = self._db.query("SELECT id, name FROM folders WHERE parent_id = ?", (parent_id,))
        target = name.casefold()
        for row in rows:
            if row["id"] != exclude and row["name"].casefold() == target:
                raise CatalogError("Папка с таким названием уже есть")

    # ---------- сканы ----------

    def add_slide(self, slide_id: str, key: str, folder_id: int, original_name: str, meta: dict, user) -> None:
        case_code, glass, stain = parse_name(original_name)
        self._db.execute(
            """
            INSERT INTO slides (id, key, folder_id, title, original_name, size, mtime, width, height,
                                objective, mpp, has_label, case_code, glass, stain, uploaded_by)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                slide_id, key, folder_id,
                None if case_code else self._default_title(folder_id),
                original_name, meta["size"], meta["mtime"], meta["width"], meta["height"],
                meta["objective"], meta["mpp"], meta["has_label"], case_code, glass, stain, user["id"],
            ),
        )

    def _default_title(self, folder_id: int) -> str:
        used = self._db.query_one("SELECT count(*) AS n FROM slides WHERE folder_id = ?", (folder_id,))
        return f"Скан {used['n'] + 1:02d}"

    def update_slide(self, slide_id: str, *, title: str | None, stain: str | None, note: str | None) -> None:
        if self.slide(slide_id) is None:
            raise CatalogError("Скан не найден")
        self._db.execute(
            "UPDATE slides SET title = ?, stain = ?, note = ? WHERE id = ?",
            (normalized(title) if title else None, (stain or "").strip() or None, (note or "").strip(), slide_id),
        )

    def move_slide(self, slide_id: str, folder_id: int) -> None:
        if self.slide(slide_id) is None:
            raise CatalogError("Скан не найден")
        if self.folder(folder_id) is None:
            raise CatalogError("Папка не найдена")
        self._db.execute("UPDATE slides SET folder_id = ? WHERE id = ?", (folder_id, slide_id))

    def delete_slide(self, slide_id: str) -> None:
        row = self._db.query_one("SELECT * FROM slides WHERE id = ?", (slide_id,))
        if row is None:
            raise CatalogError("Скан не найден")
        self._pool.release_key(row["key"])  # иначе открытый файл не удалить
        try:
            self._storage.delete(row["key"])
        except StorageUnavailable as exc:
            log.warning("Файл слайда %s не удалён: %s", slide_id, exc)
        self._db.execute("DELETE FROM slides WHERE id = ?", (slide_id,))

    # ---------- права ----------

    def set_access(self, *, folder_id: int | None, slide_id: str | None, mode: str | None, user_ids: list[int], actor) -> None:
        """Режим доступа и список выбранных пользователей для папки или скана."""
        if (folder_id is None) == (slide_id is None):
            raise CatalogError("Доступ задаётся либо папке, либо скану")
        if mode is not None and mode not in MODES:
            raise CatalogError("Неизвестный режим доступа")
        if folder_id is not None:
            row = self.folder(folder_id)
            if row is None:
                raise CatalogError("Папка не найдена")
            if row["parent_id"] is None and mode is None:
                raise CatalogError("У папки верхнего уровня режим доступа обязателен")
        elif self.slide(slide_id) is None:
            raise CatalogError("Скан не найден")

        with self._db.transaction() as conn:
            if folder_id is not None:
                conn.execute("UPDATE folders SET access_mode = ? WHERE id = ?", (mode, folder_id))
                conn.execute("DELETE FROM access_grants WHERE folder_id = ?", (folder_id,))
            else:
                conn.execute("UPDATE slides SET access_mode = ? WHERE id = ?", (mode, slide_id))
                conn.execute("DELETE FROM access_grants WHERE slide_id = ?", (slide_id,))
            if mode == "selected":
                conn.executemany(
                    "INSERT INTO access_grants (user_id, folder_id, slide_id, granted_by) VALUES (?, ?, ?, ?)",
                    [(uid, folder_id, slide_id, actor["id"]) for uid in dict.fromkeys(user_ids)],
                )

    def granted_user_ids(self, *, folder_id: int | None = None, slide_id: str | None = None) -> list[int]:
        if folder_id is not None:
            rows = self._db.query("SELECT user_id FROM access_grants WHERE folder_id = ?", (folder_id,))
        else:
            rows = self._db.query("SELECT user_id FROM access_grants WHERE slide_id = ?", (slide_id,))
        return [row["user_id"] for row in rows]

    # ---------- целостность ----------

    def check_integrity(self) -> dict:
        """Сверяет каталог с диском: файл мог пропасть после сбоя или ручного вмешательства.

        Новые файлы в хранилище не подхватываются: сканы попадают в каталог
        только через загрузку администратором.
        """
        with self._lock:
            present = {obj.key for obj in self._storage.list_slides()}
            known = self._db.query("SELECT key, missing FROM slides")
            gone = restored = 0
            for row in known:
                if row["key"] not in present and not row["missing"]:
                    self._db.execute("UPDATE slides SET missing = 1 WHERE key = ?", (row["key"],))
                    gone += 1
                elif row["key"] in present and row["missing"]:
                    self._db.execute("UPDATE slides SET missing = 0 WHERE key = ?", (row["key"],))
                    restored += 1
            self._last_check = time.monotonic()
            orphans = len(present) - len(known)
            return {"total": len(known), "missing": gone, "restored": restored, "orphan_files": max(orphans, 0)}

    def check_if_stale(self) -> None:
        if not self._last_check or time.monotonic() - self._last_check > self._check_interval:
            try:
                self.check_integrity()
            except StorageUnavailable as exc:
                log.warning("Проверка хранилища не удалась: %s", exc)
