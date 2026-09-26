"""Прогрев скана после загрузки (СК-1).

Файл Aperio хранит мелкие уровни пирамиды невыгодно для чтения: тайл обзорного
уровня при первом обращении обходится в 350 мс, миниатюра — до полусекунды, и
первое открытие скана растягивается на 1,5–3 секунды. Дальше всё быстро: готовый
тайл лежит в дисковом кэше и отдаётся сразу всем.

Поэтому сразу после приёма файла сервер в фоне готовит миниатюру и тайлы всех
обзорных уровней — до уровня, на котором тайлов не больше MAX_OVERVIEW_TILES.
Уровни растут вчетверо, так что это первые несколько уровней и несколько секунд
работы на скан.

Прогрев идёт одним потоком, по одному скану за раз, и между тайлами делает
паузу: у сервера два ядра, и просмотр другого скана не должен ждать. Сбой
прогрева ни на что не влияет — скан уже в каталоге, тайлы просто будут
готовиться по первому обращению, как раньше.
"""
from __future__ import annotations

import io
import logging
import queue
import threading
import time

from .config import TileConfig
from .slides import SlidePool, render_thumbnail, thumbnail_path
from .tilecache import TileCache, namespace

log = logging.getLogger(__name__)

MAX_OVERVIEW_TILES = 100  # на сколько тайлов уровня прогрев ещё соглашается
PAUSE_BETWEEN_TILES_S = 0.02  # уступка просмотру: прогрев не занимает ядро целиком


class Warmer:
    def __init__(self, pool: SlidePool, tile_cache: TileCache, tiles: TileConfig, thumbs_dir,
                 overview: bool = True):
        self._pool = pool
        # Настольная программа готовит только миниатюру (НП-16): в подключённой папке
        # бывает сотня сканов, а обзорные тайлы сделает первое открытие
        self._overview = overview
        self._cache = tile_cache
        self._tiles = tiles
        self._thumbs_dir = thumbs_dir
        self._queue: queue.Queue = queue.Queue()
        self._lock = threading.Lock()
        self._queued: set[str] = set()  # id сканов в очереди и в работе
        self._thread = threading.Thread(target=self._run, name="slide-warmer", daemon=True)

    # ---------- фоновая очередь ----------

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._queue.put(None)

    def enqueue(self, slide_row) -> None:
        """Поставить скан в очередь прогрева. Повторы отбрасываются."""
        if slide_row is None:
            return
        with self._lock:
            if slide_row["id"] in self._queued:
                return
            self._queued.add(slide_row["id"])
        self._queue.put(slide_row)

    def wait_idle(self, timeout: float = 120.0) -> bool:
        """Дождаться конца прогрева. Нужна тестам и команде `manage warm`."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                if not self._queued:
                    return True
            time.sleep(0.05)
        return False

    def _run(self) -> None:
        while True:
            slide_row = self._queue.get()
            if slide_row is None:
                return
            try:
                result = self.warm(slide_row)
                log.info("Прогрев скана %s: тайлов %s, за %.1f с", slide_row["id"], result["tiles"], result["seconds"])
            except Exception:  # прогрев не обязан удаваться: скан уже в каталоге
                log.exception("Прогрев скана %s не удался", slide_row["id"])
            finally:
                with self._lock:
                    self._queued.discard(slide_row["id"])

    # ---------- сама работа ----------

    def warm(self, slide_row, pause: float = PAUSE_BETWEEN_TILES_S) -> dict:
        """Готовит миниатюру и обзорные тайлы одного скана. Возвращает, сколько сделано."""
        started = time.monotonic()
        space = namespace(slide_row, self._tiles)
        made = 0
        with self._pool.acquire(slide_row["key"]) as handle:
            thumb = thumbnail_path(self._thumbs_dir, slide_row)
            if not thumb.exists():
                render_thumbnail(handle.slide, thumb)
            tiler = handle.tiler
            for level in range(tiler.level_count if self._overview else 0):
                cols, rows = tiler.tile_count(level)
                if cols * rows > MAX_OVERVIEW_TILES:
                    break  # дальше уровни только растут: это уже не обзор
                for col in range(cols):
                    for row in range(rows):
                        path = self._cache.path(space, level, col, row)
                        if path.exists():
                            continue
                        buffer = io.BytesIO()
                        tiler.get_tile(level, col, row).save(
                            buffer, "JPEG", quality=self._tiles.jpeg_quality
                        )
                        self._cache.put(path, buffer.getvalue())
                        made += 1
                        if pause:
                            time.sleep(pause)
        return {"tiles": made, "seconds": time.monotonic() - started}
