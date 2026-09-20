"""Открытые слайды и чтение метаданных из файла."""
from __future__ import annotations

import threading
import uuid
from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import openslide

from .config import TileConfig
from .storage import Storage, StorageUnavailable
from .tiles import DeepZoomTiler

STANDARD_OBJECTIVES = (2.5, 5, 10, 20, 40, 60, 80, 100)
LABEL_IMAGE = "label"  # фото этикетки стекла; macro пользователям не отдаётся
THUMBNAIL_SIZE = (320, 320)


def objective_from_mpp(mpp: float) -> float:
    """0,25 мкм/пиксель ≈ 40×, 0,5 мкм/пиксель ≈ 20×."""
    raw = 10.0 / mpp
    nearest = min(STANDARD_OBJECTIVES, key=lambda o: abs(o - raw))
    return float(nearest) if abs(nearest - raw) / nearest <= 0.15 else round(raw, 1)


def _to_float(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def read_metadata(slide: openslide.OpenSlide) -> dict:
    props = slide.properties
    mpp = _to_float(props.get(openslide.PROPERTY_NAME_MPP_X))
    objective = _to_float(props.get(openslide.PROPERTY_NAME_OBJECTIVE_POWER))
    if objective is None and mpp is not None:
        objective = objective_from_mpp(mpp)
    width, height = slide.dimensions
    return {
        "width": width,
        "height": height,
        "objective": objective,
        "mpp": mpp,
        "has_label": int(LABEL_IMAGE in slide.associated_images),
    }


def thumbnail_path(thumbs_dir: Path, slide_row) -> Path:
    """Имя файла миниатюры включает дату файла: после замены скана она пересоздаётся."""
    return thumbs_dir / f"{slide_row['id']}-{int(slide_row['mtime'])}.jpg"


def render_thumbnail(slide: openslide.OpenSlide, path: Path) -> None:
    """Готовит миниатюру для каталога. Запись через временный файл: страница
    каталога не должна получить наполовину записанный JPEG."""
    try:
        # get_thumbnail берёт изображение препарата: этикетка в миниатюру не попадает
        image = slide.get_thumbnail(THUMBNAIL_SIZE)
    except Exception as exc:
        raise StorageUnavailable(f"Ошибка чтения миниатюры: {exc}") from exc
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
    image.convert("RGB").save(tmp, "JPEG", quality=85)
    tmp.replace(path)


@dataclass
class _Handle:
    slide: openslide.OpenSlide
    tiler: DeepZoomTiler
    users: int = 0
    evicted: bool = False


class SlidePool:
    """LRU-набор открытых слайдов. Вытесненный слайд закрывается, когда его отпустят все потоки."""

    def __init__(self, storage: Storage, tiles: TileConfig, max_open: int):
        self._storage = storage
        self._tiles = tiles
        self._max_open = max_open
        self._lock = threading.Lock()
        self._handles: OrderedDict[str, _Handle] = OrderedDict()

    @contextmanager
    def acquire(self, key: str):
        handle = self._checkout(key)
        try:
            yield handle
        finally:
            with self._lock:
                handle.users -= 1
                if handle.evicted and handle.users == 0:
                    handle.slide.close()

    def _checkout(self, key: str) -> _Handle:
        with self._lock:
            handle = self._handles.get(key)
            if handle:
                self._handles.move_to_end(key)
                handle.users += 1
                return handle

        # Открытие большого файла может быть долгим, поэтому вне блокировки.
        slide = self._storage.open_slide(key)
        tiler = DeepZoomTiler(slide, self._tiles.tile_size, self._tiles.overlap)
        with self._lock:
            handle = self._handles.get(key)
            if handle:  # другой поток успел раньше
                slide.close()
            else:
                handle = self._handles[key] = _Handle(slide, tiler)
                self._evict_excess()
            self._handles.move_to_end(key)
            handle.users += 1
            return handle

    def _evict_excess(self) -> None:
        while len(self._handles) > self._max_open:
            _, old = self._handles.popitem(last=False)
            old.evicted = True
            if old.users == 0:
                old.slide.close()

    def release_key(self, key: str) -> None:
        """Закрыть слайд перед удалением файла: иначе Windows не даст его стереть."""
        with self._lock:
            handle = self._handles.pop(key, None)
            if handle is not None:
                handle.evicted = True
                if handle.users == 0:
                    handle.slide.close()

    def close_all(self) -> None:
        with self._lock:
            for handle in self._handles.values():
                handle.evicted = True
                if handle.users == 0:
                    handle.slide.close()
            self._handles.clear()
