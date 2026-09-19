"""Права доступа: кто какие папки и сканы видит.

Режим доступа («только администраторы», «все пользователи», «выбранные»)
задаётся у папки или у отдельного скана. Свой режим заменяет унаследованный,
а не дополняет его: внутри папки «для всех» можно закрыть одну подпапку.
Разрешения для режима «выбранные» действуют там же, где задан сам режим.
"""
from __future__ import annotations

from dataclasses import dataclass

from .db import Database

ADMINS_ONLY = "admins"
EVERYONE = "all"
SELECTED = "selected"
MODES = (ADMINS_ONLY, EVERYONE, SELECTED)

# Дерево по ТЗ не глубже 5 уровней; запас на случай испорченных данных,
# чтобы цикл «папка сама себе предок» не подвесил сервер.
MAX_DEPTH = 32


@dataclass(frozen=True)
class Source:
    """Откуда взят действующий режим: сам слайд или папка выше."""

    kind: str  # 'slide' | 'folder'
    id: object
    mode: str
    inherited: bool


class AccessIndex:
    """Права одного пользователя. Строится на запрос: две выборки из локальной базы."""

    def __init__(self, db: Database, user):
        self.user = user
        self.is_admin = user is not None and user["role"] == "admin"
        folders = db.query("SELECT id, parent_id, access_mode FROM folders")
        self._parent = {row["id"]: row["parent_id"] for row in folders}
        self._mode = {row["id"]: row["access_mode"] for row in folders}
        self._folder_grants: set[int] = set()
        self._slide_grants: set[str] = set()
        if user is not None and not self.is_admin:
            for row in db.query("SELECT folder_id, slide_id FROM access_grants WHERE user_id = ?", (user["id"],)):
                if row["folder_id"] is not None:
                    self._folder_grants.add(row["folder_id"])
                else:
                    self._slide_grants.add(row["slide_id"])

    # ---------- действующий режим ----------

    def folder_source(self, folder_id: int | None) -> Source | None:
        """Ближайшая папка вверх по дереву, у которой режим задан явно."""
        inherited = False
        for _ in range(MAX_DEPTH):
            if folder_id is None:
                return None
            mode = self._mode.get(folder_id)
            if mode is not None:
                return Source("folder", folder_id, mode, inherited)
            folder_id = self._parent.get(folder_id)
            inherited = True
        return None

    def slide_source(self, slide_row) -> Source | None:
        if slide_row["access_mode"] is not None:
            return Source("slide", slide_row["id"], slide_row["access_mode"], False)
        source = self.folder_source(slide_row["folder_id"])
        return None if source is None else Source(source.kind, source.id, source.mode, True)

    # ---------- проверки ----------

    def _allows(self, source: Source | None) -> bool:
        if self.is_admin:
            return True
        if source is None or self.user is None:
            return False  # режим не найден: безопасный отказ
        if source.mode == EVERYONE:
            return True
        if source.mode == SELECTED:
            if source.kind == "slide":
                return source.id in self._slide_grants
            return source.id in self._folder_grants
        return False

    def can_view_slide(self, slide_row) -> bool:
        return self._allows(self.slide_source(slide_row))

    def visible_folder_ids(self, slide_rows) -> set[int]:
        """Папки, внутри которых на любом уровне есть хотя бы один доступный скан.

        Пустые папки видит только администратор: пользователю не за чем знать,
        что папка существует, если смотреть в ней нечего.
        """
        if self.is_admin:
            return set(self._parent)
        visible: set[int] = set()
        for row in slide_rows:
            folder_id = row["folder_id"]
            if folder_id in visible or not self.can_view_slide(row):
                continue
            while folder_id is not None and folder_id not in visible:
                visible.add(folder_id)
                folder_id = self._parent.get(folder_id)
        return visible

    def ancestors(self, folder_id: int | None) -> list[int]:
        """Путь от корня до папки включительно."""
        chain: list[int] = []
        for _ in range(MAX_DEPTH):
            if folder_id is None:
                break
            if folder_id in chain:  # испорченное дерево
                break
            chain.append(folder_id)
            folder_id = self._parent.get(folder_id)
        return list(reversed(chain))

    def descendants(self, folder_id: int) -> set[int]:
        """Папка и всё, что внутри неё. Нужна при перемещении и удалении."""
        children: dict[int | None, list[int]] = {}
        for child, parent in self._parent.items():
            children.setdefault(parent, []).append(child)
        result: set[int] = set()
        stack = [folder_id]
        while stack:
            current = stack.pop()
            if current in result:
                continue
            result.add(current)
            stack.extend(children.get(current, ()))
        return result
