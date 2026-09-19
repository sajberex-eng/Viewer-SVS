"""Открытые слайды, метаданные и синхронизация каталога с хранилищем."""
from __future__ import annotations

import hashlib
import logging
import re
import threading
import time
from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass

import openslide

from .config import TileConfig
from .db import Database
from .storage import Storage, StorageUnavailable, StoredObject
from .tiles import DeepZoomTiler

log = logging.getLogger(__name__)

# <ИД пациента>_<номер стекла>_<окраска>.svs, например P004512_S03_HE.svs
NAME_PATTERN = re.compile(r"^([A-Za-z0-9-]+)_([A-Za-z0-9-]+)_([A-Za-z0-9-]+)\.svs$", re.IGNORECASE)

STANDARD_OBJECTIVES = (2.5, 5, 10, 20, 40, 60, 80, 100)


def slide_id_for(key: str) -> str:
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]


def parse_name(key: str) -> tuple[str | None, str | None, str | None]:
    match = NAME_PATTERN.match(key.rsplit("/", 1)[-1])
    return match.groups() if match else (None, None, None)


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
    return {"width": width, "height": height, "objective": objective, "mpp": mpp}


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

    def close_all(self) -> None:
        with self._lock:
            for handle in self._handles.values():
                handle.evicted = True
                if handle.users == 0:
                    handle.slide.close()
            self._handles.clear()


class Catalog:
    def __init__(self, db: Database, storage: Storage, pool: SlidePool, sync_minutes: int):
        self._db = db
        self._storage = storage
        self._pool = pool
        self._sync_interval = sync_minutes * 60
        self._sync_lock = threading.Lock()
        self._last_sync = 0.0

    def sync(self) -> dict:
        """Находит новые и изменённые файлы, читает их метаданные, помечает пропавшие."""
        with self._sync_lock:
            objects = self._storage.list_slides()
            known = {row["key"]: row for row in self._db.query("SELECT key, size, mtime, missing FROM slides")}
            added = failed = 0
            for obj in objects:
                row = known.get(obj.key)
                if row and row["size"] == obj.size and row["mtime"] == obj.mtime:
                    if row["missing"]:
                        self._db.execute("UPDATE slides SET missing = 0 WHERE key = ?", (obj.key,))
                    continue
                try:
                    self._upsert(obj)
                    added += 1
                except StorageUnavailable as exc:
                    log.warning("Слайд %s пропущен: %s", slide_id_for(obj.key), exc)
                    failed += 1
            present = {obj.key for obj in objects}
            for key in known.keys() - present:
                self._db.execute("UPDATE slides SET missing = 1 WHERE key = ?", (key,))
            self._last_sync = time.monotonic()
            return {"total": len(objects), "updated": added, "failed": failed}

    def _upsert(self, obj: StoredObject) -> None:
        with self._pool.acquire(obj.key) as handle:
            meta = read_metadata(handle.slide)
        case_code, glass, stain = parse_name(obj.key)
        self._db.execute(
            """
            INSERT INTO slides (id, key, size, mtime, width, height, objective, mpp, case_code, glass, stain)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                size = excluded.size, mtime = excluded.mtime, width = excluded.width,
                height = excluded.height, objective = excluded.objective, mpp = excluded.mpp,
                missing = 0
            """,
            (slide_id_for(obj.key), obj.key, obj.size, obj.mtime, meta["width"], meta["height"],
             meta["objective"], meta["mpp"], case_code, glass, stain),
        )

    def sync_if_stale(self) -> None:
        if time.monotonic() - self._last_sync > self._sync_interval or not self._last_sync:
            self.sync()

    def list(self) -> list:
        return self._db.query("SELECT * FROM slides WHERE missing = 0 ORDER BY case_code IS NULL, case_code, glass, id")

    def get(self, slide_id: str):
        return self._db.query_one("SELECT * FROM slides WHERE id = ? AND missing = 0", (slide_id,))

    def siblings(self, row) -> list:
        """Стёкла того же случая; файлы с именем не по шаблону образуют общую группу."""
        if row["case_code"] is None:
            return self._db.query("SELECT * FROM slides WHERE case_code IS NULL AND missing = 0 ORDER BY id")
        return self._db.query(
            "SELECT * FROM slides WHERE case_code = ? AND missing = 0 ORDER BY glass, id", (row["case_code"],)
        )
