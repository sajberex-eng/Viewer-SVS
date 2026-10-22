"""Самопроверка собранной программы: HemCenterHistoDigital --selftest <скан.svs> <отчёт.txt>.

Окно не открывается. Проверяет то, что в обычных тестах из исходников не видно, а в
собранной программе ломается первым: упаковку OpenSlide, Numba и OpenCV, дочерние
процессы (spawn + freeze_support) и pywebview. Запускается на CI для Windows и macOS
после сборки (.github/workflows/desktop.yml). Код возврата 0 — всё в порядке.
"""
from __future__ import annotations

import multiprocessing
import os
import sys
from pathlib import Path


def child(queue) -> None:
    """Работает в дочернем процессе — как процесс расчёта клеточности."""
    from server import ij_watershed  # noqa: F401  (Numba в дочернем процессе)

    queue.put("дочерний процесс ответил")


def run(scan: Path, report: Path) -> int:
    os.environ.setdefault("NUMBA_CACHE_DIR", str(report.parent / "numba"))
    report.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    failed = False

    def write(line: str) -> None:
        """Отчёт пишется построчно: если самопроверка зависнет, видно, на каком шаге."""
        lines.append(line)
        with open(report, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")

    report.write_text("", encoding="utf-8")
    write(f"начало: Python {sys.version.split()[0]}, frozen={getattr(sys, 'frozen', False)}")

    def step(name: str, action) -> None:
        nonlocal failed
        write(f"... {name}")
        try:
            write(f"[OK] {name}: {action()}")
        except Exception as exc:  # отчёт нужен именно с причиной
            failed = True
            write(f"[СБОЙ] {name}: {type(exc).__name__}: {exc}")

    def read_scan() -> str:
        import openslide

        with openslide.OpenSlide(str(scan)) as slide:
            pixels = slide.read_region((0, 0), 0, (64, 64))
            return f"{slide.dimensions}, область {pixels.size}, OpenSlide {openslide.__library_version__}"

    def make_tile() -> str:
        import openslide

        from server.tiles import DeepZoomTiler

        with openslide.OpenSlide(str(scan)) as slide:
            tiler = DeepZoomTiler(slide, 254, 1)
            tile = tiler.get_tile(tiler.level_count - 1, 0, 0)
            return f"тайл {tile.size}"

    def watershed() -> str:
        import cv2
        import numpy as np

        from server import cellularity as c

        two = np.zeros((60, 90), np.uint8)
        cv2.circle(two, (30, 30), 18, 1, -1)
        cv2.circle(two, (58, 30), 18, 1, -1)
        parts = cv2.connectedComponents(c.watershed(two), connectivity=8)[0] - 1
        if parts != 2:
            raise AssertionError(f"частиц {parts}, ожидалось 2")
        return "два слипшихся круга разделены (Numba, OpenCV)"

    def spawn() -> str:
        context = multiprocessing.get_context("spawn")
        queue = context.Queue()
        process = context.Process(target=child, args=(queue,), daemon=True)
        process.start()
        answer = queue.get(timeout=120)
        process.join(30)
        return answer

    def window_library() -> str:
        import webview

        return f"pywebview {getattr(webview, '__version__', '?')}"

    def kfb() -> str:
        from server import kfb as kfb_module

        return "доступен" if kfb_module.AVAILABLE else f"недоступен ({kfb_module.unavailable_reason()})"

    for name, action in (("чтение скана", read_scan), ("тайл", make_tile), ("Numba и OpenCV", watershed),
                         ("дочерний процесс", spawn), ("окно", window_library), ("KFB", kfb)):
        step(name, action)
    if getattr(sys, "stderr", None) is not None:  # в безоконной сборке Windows stderr нет
        print("\n".join(lines), file=sys.stderr)
    return 1 if failed else 0
