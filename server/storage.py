"""Адаптер хранилища: «сохранить, открыть, удалить, сколько места осталось».

Сканы загружает администратор через веб-интерфейс, поэтому папкой управляет
только приложение: файлы лежат под внутренними идентификаторами
(`ab/ab12cd34ef56.svs`), исходные имена остаются в базе. Необязательный этап с
S3-совместимым бакетом добавит вторую реализацию, не трогая остальной код.
"""
from __future__ import annotations

import os
import shutil
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

import openslide

from .config import BASE_DIR, StorageConfig

SLIDE_EXTENSION = ".svs"


class StorageUnavailable(Exception):
    """Хранилище недоступно или файл из него не читается."""


class NotEnoughSpace(Exception):
    """На диске не хватает места для нового файла."""


@dataclass(frozen=True)
class StoredObject:
    key: str  # путь относительно корня хранилища, разделитель «/»
    size: int
    mtime: float


@dataclass(frozen=True)
class DiskSpace:
    total: int
    used: int
    free: int


def key_for(slide_id: str) -> str:
    """Ключ хранилища по внутреннему идентификатору слайда.

    Первые два символа образуют подпапку: в одной папке не копятся тысячи файлов.
    """
    return f"{slide_id[:2]}/{slide_id}{SLIDE_EXTENSION}"


class Storage(ABC):
    @abstractmethod
    def list_slides(self) -> list[StoredObject]: ...

    @abstractmethod
    def open_slide(self, key: str) -> openslide.OpenSlide: ...

    @abstractmethod
    def put(self, key: str, source: Path) -> StoredObject: ...

    @abstractmethod
    def delete(self, key: str) -> bool: ...

    @abstractmethod
    def space(self) -> DiskSpace: ...


class LocalFolderStorage(Storage):
    def __init__(self, config: StorageConfig):
        path = Path(config.root)
        self.root = path if path.is_absolute() else BASE_DIR / path
        self.reserve_bytes = int(config.reserve_gb * 1e9)
        self.max_upload_bytes = int(config.max_upload_gb * 1e9)
        self.warn_free_bytes = int(config.warn_free_gb * 1e9)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        path = (self.root / key).resolve()
        root = self.root.resolve()
        if not path.is_relative_to(root):
            raise StorageUnavailable("Ключ слайда выходит за пределы хранилища")
        return path

    def list_slides(self) -> list[StoredObject]:
        if not self.root.is_dir():
            raise StorageUnavailable(f"Папка хранилища недоступна: {self.root}")
        objects = []
        try:
            for path in self.root.glob(f"**/*{SLIDE_EXTENSION}"):
                if path.is_file():
                    stat = path.stat()
                    key = path.relative_to(self.root).as_posix()
                    objects.append(StoredObject(key, stat.st_size, stat.st_mtime))
        except OSError as exc:
            raise StorageUnavailable(f"Ошибка чтения хранилища: {exc}") from exc
        return sorted(objects, key=lambda o: o.key)

    def open_slide(self, key: str) -> openslide.OpenSlide:
        try:
            return openslide.OpenSlide(self._path(key))
        except (OSError, openslide.OpenSlideError) as exc:
            raise StorageUnavailable(f"Не удалось открыть слайд: {exc}") from exc

    def put(self, key: str, source: Path) -> StoredObject:
        """Переносит принятый файл в хранилище.

        Именно переносит, а не копирует: скан 3 ГБ иначе занимал бы 6 ГБ,
        а на диске 30 ГБ это заметно.
        """
        target = self._path(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.replace(source, target)  # атомарно в пределах одного диска
        except OSError as exc:
            raise StorageUnavailable(f"Не удалось поместить файл в хранилище: {exc}") from exc
        stat = target.stat()
        return StoredObject(key, stat.st_size, stat.st_mtime)

    def delete(self, key: str) -> bool:
        try:
            self._path(key).unlink()
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise StorageUnavailable(f"Не удалось удалить файл: {exc}") from exc
        return True

    def space(self) -> DiskSpace:
        try:
            usage = shutil.disk_usage(self.root)
        except OSError as exc:
            raise StorageUnavailable(f"Не удалось узнать свободное место: {exc}") from exc
        return DiskSpace(usage.total, usage.used, usage.free)

    def check_can_accept(self, size: int) -> None:
        """Хватит ли места на файл указанного размера с учётом неприкосновенного резерва."""
        if size > self.max_upload_bytes:
            raise NotEnoughSpace(
                f"Файл больше допустимого размера ({self.max_upload_bytes / 1e9:.0f} ГБ)"
            )
        free = self.space().free
        if free - size < self.reserve_bytes:
            need = size + self.reserve_bytes - free
            raise NotEnoughSpace(
                f"Не хватает места: освободите не менее {need / 1e9:.1f} ГБ"
            )


def create_storage(config: StorageConfig) -> Storage:
    if config.type == "local":
        return LocalFolderStorage(config)
    raise ValueError(f"config: неизвестный тип хранилища «{config.type}»")
