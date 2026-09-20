"""Дисковый кэш готовых тайлов с лимитом объёма и вытеснением по давности использования."""
from __future__ import annotations

import logging
import os
import threading
import time
import uuid
from pathlib import Path

log = logging.getLogger(__name__)

CLEANUP_INTERVAL_S = 300
CLEANUP_TARGET = 0.9  # после очистки остаётся не больше 90 % лимита
TOUCH_INTERVAL_S = 3600  # отметка «использован» обновляется не чаще раза в час (СК-5)


def namespace(slide_row, tiles) -> str:
    """Пространство имён тайлов слайда: включает размер и дату файла и параметры
    нарезки, поэтому после замены файла или смены настроек старые тайлы не всплывут."""
    return (
        f"{slide_row['id']}-{int(slide_row['mtime'])}-{slide_row['size']}"
        f"-{tiles.tile_size}-{tiles.overlap}-{tiles.jpeg_quality}"
    )


class TileCache:
    def __init__(self, root: Path, max_bytes: int):
        self.root = root
        self.max_bytes = max_bytes
        root.mkdir(parents=True, exist_ok=True)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._janitor, name="tile-cache-janitor", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def path(self, namespace: str, level: int, col: int, row: int) -> Path:
        return self.root / namespace / str(level) / f"{col}_{row}.jpg"

    def get(self, path: Path) -> Path | None:
        try:
            stat = os.stat(path)
        except OSError:
            return None
        # Отметка «недавно использован» нужна вытеснению, но писать её на каждый
        # тайл — лишняя запись на диск: часового разрешения хватает (СК-5).
        if time.time() - stat.st_mtime > TOUCH_INTERVAL_S:
            try:
                os.utime(path)
            except OSError:
                pass
        return path

    def put(self, path: Path, data: bytes) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
            tmp.write_bytes(data)
            os.replace(tmp, path)
        except OSError as exc:  # кэш не должен ломать выдачу тайла
            log.warning("Тайл не записан в кэш: %s", exc)

    def cleanup(self) -> None:
        files = []
        total = 0
        for dirpath, _, names in os.walk(self.root):
            for name in names:
                full = os.path.join(dirpath, name)
                try:
                    stat = os.stat(full)
                except OSError:
                    continue
                files.append((stat.st_mtime, stat.st_size, full))
                total += stat.st_size
        if total <= self.max_bytes:
            return
        files.sort()
        target = self.max_bytes * CLEANUP_TARGET
        for _, size, full in files:
            if total <= target:
                break
            try:
                os.remove(full)
                total -= size
            except OSError:
                pass
        log.info("Кэш тайлов очищен до %.1f ГБ", total / 1e9)

    def _janitor(self) -> None:
        while not self._stop.wait(CLEANUP_INTERVAL_S):
            try:
                self.cleanup()
            except Exception:
                log.exception("Ошибка очистки кэша тайлов")
