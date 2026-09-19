"""Адаптер хранилища: единый интерфейс «перечислить слайды / открыть слайд по ключу».

Основной вариант по ТЗ: LocalFolderStorage, папка сканов на диске VPS, куда
администратор загружает файлы по SFTP. Необязательный этап 3 добавит реализацию
для S3-совместимого бакета (tiffslide + fsspec); ключи слайдов в каталоге
при этом не меняются.
"""
from __future__ import annotations

import os
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

import openslide

from .config import BASE_DIR, StorageConfig

SLIDE_EXTENSIONS = {".svs"}


class StorageUnavailable(Exception):
    """Хранилище недоступно или файл из него не читается."""


@dataclass(frozen=True)
class StoredObject:
    key: str  # путь относительно корня хранилища, разделитель «/»
    size: int
    mtime: float


class Storage(ABC):
    @abstractmethod
    def list_slides(self) -> list[StoredObject]: ...

    @abstractmethod
    def open_slide(self, key: str) -> openslide.OpenSlide: ...


class LocalFolderStorage(Storage):
    def __init__(self, root: str, recursive: bool = False):
        path = Path(root)
        self.root = path if path.is_absolute() else BASE_DIR / path
        self.recursive = recursive

    def list_slides(self) -> list[StoredObject]:
        if not self.root.is_dir():
            raise StorageUnavailable(f"Папка хранилища недоступна: {self.root}")
        pattern = "**/*" if self.recursive else "*"
        objects = []
        try:
            for path in self.root.glob(pattern):
                if path.suffix.lower() in SLIDE_EXTENSIONS and path.is_file():
                    stat = path.stat()
                    key = path.relative_to(self.root).as_posix()
                    objects.append(StoredObject(key, stat.st_size, stat.st_mtime))
        except OSError as exc:
            raise StorageUnavailable(f"Ошибка чтения хранилища: {exc}") from exc
        return sorted(objects, key=lambda o: o.key)

    def open_slide(self, key: str) -> openslide.OpenSlide:
        path = (self.root / key).resolve()
        if os.path.commonpath([path, self.root.resolve()]) != str(self.root.resolve()):
            raise StorageUnavailable("Ключ слайда выходит за пределы хранилища")
        try:
            return openslide.OpenSlide(path)
        except (OSError, openslide.OpenSlideError) as exc:
            raise StorageUnavailable(f"Не удалось открыть слайд: {exc}") from exc


def create_storage(config: StorageConfig) -> Storage:
    if config.type == "local":
        return LocalFolderStorage(config.root, config.recursive)
    raise ValueError(f"config: неизвестный тип хранилища «{config.type}»")
