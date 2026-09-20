"""Приём сканов от администратора: частями, с докачкой после обрыва связи.

Файл дописывается в конец одного и того же временного файла на том же диске,
что и хранилище. Поэтому докачка знает, сколько уже принято, а готовый файл
переносится в хранилище без копирования: скан 3 ГБ не занимает 6 ГБ.
"""
from __future__ import annotations

import hashlib
import logging
import secrets
import time
from dataclasses import dataclass
from pathlib import Path

from . import kfb
from .catalog import Catalog, CatalogError, new_slide_id
from .db import Database
from .slides import read_metadata
from .storage import KFB_EXTENSION, SLIDE_EXTENSIONS, NotEnoughSpace, Storage, StorageUnavailable, key_for, slide_extension

log = logging.getLogger(__name__)

PART_SIZE = 8 * 1024 * 1024  # 8 МБ: ТЗ Х-5 допускает 8–16
STALE_DAYS = 3  # ТЗ Х-13, ужесточено под диск 30 ГБ


@dataclass(frozen=True)
class UploadState:
    id: str
    folder_id: int
    original_name: str
    size: int
    received: int

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "folder_id": self.folder_id,
            "original_name": self.original_name,
            "size": self.size,
            "received": self.received,
            "part_size": PART_SIZE,
        }


class UploadError(Exception):
    """Ошибка загрузки, понятная администратору.

    Код ответа говорит странице, лечится ли ошибка повтором (ЗГ-1):
    409 — сверить принятое с сервером и продолжать, 503 — подождать;
    400, 403, 404 — повтор не поможет, загрузка останавливается.
    """

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


class Uploads:
    def __init__(self, db: Database, storage: Storage, catalog: Catalog, directory: Path):
        self._db = db
        self._storage = storage
        self._catalog = catalog
        self._dir = directory
        self._dir.mkdir(parents=True, exist_ok=True)

    def _path(self, upload_id: str) -> Path:
        return self._dir / f"{upload_id}.part"

    def _row(self, upload_id: str):
        """Загрузка по идентификатору.

        Чужую загрузку продолжает любой администратор (ЗГ-4): человек, начавший
        её, может уйти в отпуск, а файл на 80 % занимает место на диске. Кто
        продолжил — видно в журнале.
        """
        row = self._db.query_one("SELECT * FROM uploads WHERE id = ?", (upload_id,))
        if row is None:
            raise UploadError("Загрузка не найдена: возможно, она была отменена", 404)
        return row

    def pending(self, user=None) -> list[dict]:
        """Все незавершённые загрузки: маршрут доступен только администраторам."""
        rows = self._db.query("SELECT * FROM uploads ORDER BY started_at")
        return [
            UploadState(r["id"], r["folder_id"], r["original_name"], r["size"], r["received"]).as_dict()
            for r in rows
        ]

    def start(self, folder_id: int, original_name: str, size: int, user) -> dict:
        name = original_name.strip().replace("\\", "/").rsplit("/", 1)[-1]
        extension = slide_extension(name)
        if extension is None:
            raise UploadError(f"Этот формат не принимается. Можно: {', '.join(SLIDE_EXTENSIONS)}")
        if extension == KFB_EXTENSION and not kfb.AVAILABLE:
            # Проверка до передачи: иначе гигабайты уйдут зря и отклонятся в конце
            raise UploadError("Чтение KFB на этом сервере не установлено")
        if size <= 0:
            raise UploadError("Файл пуст")
        if self._catalog.folder(folder_id) is None:
            raise UploadError("Папка не найдена")

        # Повторное начало той же загрузки — это докачка после обрыва связи.
        # Начавший её администратор не важен (ЗГ-4): файл опознаётся по папке,
        # имени и размеру.
        existing = self._db.query_one(
            "SELECT * FROM uploads WHERE folder_id = ? AND original_name = ? AND size = ?",
            (folder_id, name, size),
        )
        if existing is not None:
            received = self._actual_size(existing["id"])
            self._db.execute(
                "UPDATE uploads SET received = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (received, existing["id"]),
            )
            state = UploadState(existing["id"], folder_id, name, size, received).as_dict()
            # Для журнала: продолжает не тот, кто начинал
            state["started_by"] = existing["user_id"] if existing["user_id"] != user["id"] else None
            return state

        try:
            self._storage.check_can_accept(size + self._reserved_by_queue())
        except NotEnoughSpace as exc:
            raise UploadError(str(exc)) from exc

        upload_id = secrets.token_hex(8)
        self._path(upload_id).touch()
        self._db.execute(
            "INSERT INTO uploads (id, folder_id, original_name, size, user_id) VALUES (?, ?, ?, ?, ?)",
            (upload_id, folder_id, name, size, user["id"]),
        )
        return UploadState(upload_id, folder_id, name, size, 0).as_dict()

    def _reserved_by_queue(self) -> int:
        """Сколько места уже обещано другим незавершённым загрузкам."""
        row = self._db.query_one("SELECT coalesce(sum(size - received), 0) AS n FROM uploads")
        return int(row["n"])

    def _actual_size(self, upload_id: str) -> int:
        path = self._path(upload_id)
        return path.stat().st_size if path.exists() else 0

    def verify_tail(self, upload_id: str, checksum: str) -> dict:
        """Сверяет последнюю принятую часть с тем же участком выбранного файла (ЗГ-5).

        После перезапуска браузера человек выбирает файл заново, и это может
        оказаться другой файл с тем же именем и размером. Дописывать к нему
        нельзя: получится мусор, который откроется как испорченный скан.
        Сравнивается хвост длиной в одну часть — этого достаточно, а читать
        гигабайты ради проверки незачем.
        """
        self._row(upload_id)
        received = self._actual_size(upload_id)
        length = min(PART_SIZE, received)
        if not length:  # принятого нет: сверять нечего, файл пишется с начала
            return {"received": 0, "length": 0, "match": True}
        digest = hashlib.sha256()
        with self._path(upload_id).open("rb") as handle:
            handle.seek(received - length)
            digest.update(handle.read(length))
        return {"received": received, "length": length, "match": digest.hexdigest() == checksum}

    def accept_part(self, upload_id: str, offset: int, data: bytes, checksum: str | None, user) -> dict:
        row = self._row(upload_id)
        path = self._path(upload_id)
        received = self._actual_size(upload_id)
        if offset != received:
            # Клиент отстал или забежал вперёд: сообщаем, откуда продолжать
            raise UploadError(f"Часть не на своём месте, продолжайте с байта {received}", 409)
        if received + len(data) > row["size"]:
            raise UploadError("Передано больше, чем заявленный размер файла")
        if checksum and hashlib.sha256(data).hexdigest() != checksum:
            raise UploadError("Часть повреждена при передаче, повторите её", 409)

        with path.open("ab") as handle:
            handle.write(data)
        received += len(data)
        self._db.execute(
            "UPDATE uploads SET received = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (received, upload_id),
        )
        return {"received": received, "size": row["size"]}

    def complete(self, upload_id: str, user) -> str:
        """Проверяет принятый файл и добавляет его в каталог. Возвращает id скана."""
        row = self._row(upload_id)
        path = self._path(upload_id)
        received = self._actual_size(upload_id)
        if received != row["size"]:
            raise UploadError(f"Файл принят не полностью: {received} из {row['size']} байт", 409)

        slide_id = new_slide_id()
        key = key_for(slide_id, slide_extension(row["original_name"]))
        try:
            stored = self._storage.put(key, path)
        except StorageUnavailable as exc:
            raise UploadError(str(exc), 503) from exc

        # Метаданные читаются уже из хранилища: так проверяется и то, что файл
        # на месте, и то, что он вообще открывается как скан.
        try:
            with self._storage.open_slide(key) as slide:
                meta = read_metadata(slide)
        except StorageUnavailable as exc:
            self._storage.delete(key)
            self._forget(upload_id)
            raise UploadError(f"Файл не открывается как скан: {exc}") from exc

        meta["size"] = stored.size
        meta["mtime"] = stored.mtime
        try:
            self._catalog.add_slide(slide_id, key, row["folder_id"], row["original_name"], meta, user)
        except CatalogError:
            self._storage.delete(key)
            raise
        self._forget(upload_id)
        return slide_id

    def cancel(self, upload_id: str, user=None) -> None:
        self._row(upload_id)
        self._forget(upload_id)

    def _forget(self, upload_id: str) -> None:
        self._path(upload_id).unlink(missing_ok=True)
        self._db.execute("DELETE FROM uploads WHERE id = ?", (upload_id,))

    def cleanup_stale(self) -> dict:
        """Брошенные загрузки занимают место, которого на диске 30 ГБ немного.

        Загрузка считается брошенной, если её файл не пополнялся дольше
        STALE_DAYS. Пока администратор её продолжает, время обновляется, и
        уборка её не тронет.
        """
        deadline = time.time() - STALE_DAYS * 86400
        removed = freed = 0
        for row in self._db.query("SELECT id FROM uploads"):
            path = self._path(row["id"])
            if not path.exists():
                self._forget(row["id"])  # файла нет: запись бессмысленна
                removed += 1
            elif path.stat().st_mtime < deadline:
                freed += path.stat().st_size
                self._forget(row["id"])
                removed += 1
        # Файлы без записи в базе остаются после сбоя в середине удаления
        known = {row["id"] for row in self._db.query("SELECT id FROM uploads")}
        orphans = 0
        for path in self._dir.glob("*.part"):
            if path.stem not in known:
                freed += path.stat().st_size
                path.unlink(missing_ok=True)
                orphans += 1
        if removed or orphans:
            log.info("Уборка загрузок: брошенных %s, ничьих файлов %s, освобождено %.1f ГБ",
                     removed, orphans, freed / 1e9)
        return {"removed": removed, "orphan_files": orphans, "freed_bytes": freed}
