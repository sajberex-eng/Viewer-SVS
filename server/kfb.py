"""Чтение сканов KFBio (.kfb) через библиотеку ASlide.

OpenSlide формат KFB не читает. ASlide (github.com/MrPeterJin/ASlide, GPL-3.0)
оборачивает закрытую библиотеку KFBio (libkfbslide.so) и повторяет интерфейс
OpenSlide. Библиотеки KFBio есть только под Linux: на Windows модуль просто
недоступен, и загрузка .kfb отклоняется с понятной причиной. Решение заказчика
от 2026-09-19; риски (закрытый SDK без подтверждённой лицензии, GPL) записаны
в CLAUDE.md.

В образ ставится только KFB-часть ASlide, без её тяжёлых зависимостей
(см. Dockerfile), поэтому импортируется модуль напрямую, а не Aslide.Slide.
"""
from __future__ import annotations

import ctypes
import io
import logging
import sys
import threading

from PIL import Image

log = logging.getLogger(__name__)

try:  # на Windows и без установленного ASlide чтения KFB нет
    from Aslide.kfb import kfb_lowlevel
    from Aslide.kfb.kfb_slide import KfbSlide
except (ImportError, OSError) as exc:  # OSError: не загрузилась закрытая .so
    KfbSlide = kfb_lowlevel = None
    _unavailable_reason = str(exc)
else:
    _unavailable_reason = ""

AVAILABLE = KfbSlide is not None


def unavailable_reason() -> str:
    return _unavailable_reason


class _AssociatedImages:
    """Прикреплённые изображения (этикетка и т. п.), чтение под замком скана.

    ASlide здесь не используется: она освобождает буфер изображения после
    чтения, а буфер принадлежит библиотеке KFBio (адрес при каждом чтении один
    и тот же). Второе чтение этикетки роняло весь процесс с «munmap_chunk():
    invalid pointer». Проверено на настоящем скане 2026-09-19. Поэтому байты
    копируются, а буфер не трогается.
    """

    def __init__(self, handle, lock: threading.Lock):
        self._handle = handle
        self._lock = lock
        with lock:
            self._names = tuple(kfb_lowlevel.kfbslide_get_associated_image_names(handle))

    def __contains__(self, name) -> bool:
        return name in self._names

    def __iter__(self):
        return iter(self._names)

    def __getitem__(self, name) -> Image.Image:
        if name not in self._names:
            raise KeyError(name)
        with self._lock:
            _, length = kfb_lowlevel.kfbslide_get_associated_image_dimensions(self._handle, name)
            pixel = ctypes.POINTER(ctypes.c_ubyte)()
            kfb_lowlevel._kfbslide_read_associated_image(self._handle, name, ctypes.byref(pixel))
            if not pixel or length <= 0:
                raise KeyError(name)
            data = ctypes.string_at(pixel, length)  # копия; буфер остаётся у библиотеки
        image = Image.open(io.BytesIO(data))
        image.load()
        return image


class KfbFile:
    """Скан KFB с интерфейсом, которым пользуется сервис (как у openslide.OpenSlide).

    Неизвестно, выдерживает ли библиотека KFBio одновременное чтение одного
    файла из нескольких потоков (OpenSlide выдерживает). Поэтому все обращения
    к файлу идут под замком: медленнее при наплыве запросов, но без риска
    уронить процесс.
    """

    def __init__(self, path: str):
        if KfbSlide is None:
            raise OSError(f"Чтение KFB недоступно: {_unavailable_reason}")
        self._lock = threading.Lock()
        self._slide = KfbSlide(path)
        # Всё, что не меняется, читаем один раз
        self.level_count = self._slide.level_count
        self.level_dimensions = self._slide.level_dimensions
        self.level_downsamples = self._slide.level_downsamples
        self.dimensions = self.level_dimensions[0] if self.level_dimensions else (0, 0)
        self.properties = dict(self._slide.properties)
        self.associated_images = _AssociatedImages(self._slide._osr, self._lock)

    def read_region(self, location, level, size) -> Image.Image:
        with self._lock:
            return self._slide.read_region(location, level, size)

    def get_thumbnail(self, size) -> Image.Image:
        """RGB-миниатюра с сохранением пропорций, как у OpenSlide."""
        level = self.level_count - 1
        with self._lock:
            region = self._slide.read_region((0, 0), level, self.level_dimensions[level])
        image = Image.new("RGB", region.size, "white")
        image.paste(region, mask=region.getchannel("A") if region.mode == "RGBA" else None)
        image.thumbnail(size, Image.Resampling.LANCZOS)
        return image

    def close(self) -> None:
        with self._lock:
            self._slide.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        self.close()


if sys.platform == "win32":
    # Windows (настольная программа): ASlide там нет, KFB читает своя обёртка над
    # библиотекой KFBio из установленной KFSlideOS (docs/TOR-desktop.md, раздел 7)
    from .kfb_win import AVAILABLE, KfbFile, unavailable_reason  # noqa: F401, E402
