"""Расчёт клеточности в отдельном процессе (КЛ-7, раздел 13.8 ТЗ).

Зачем отдельный процесс: память после расчёта освобождается целиком (в долгоживущем
процессе сервиса освобождённые страницы NumPy остаются у распределителя), просмотр у
других пользователей не тормозит (у процесса пониженный приоритет), а у KFB — своя копия
закрытой библиотеки и свой замок, так что просмотр KFB во время расчёта не останавливается
(КЛ-10). Расчёты идут по одному: остальные ждут в очереди.

Пределы: память — по оценке до запуска (estimate_peak_mb) и наблюдателем за RSS процесса
каждые четверть секунды, на Linux ещё RLIMIT_DATA (втрое больше, виртуальная память)
как запасной предел внутри процесса;
время — deadline. Превышение — процесс убивается, вызывающему уходит понятная ошибка.

Ход: процесс шлёт по каналу (фрагмент, число фрагментов, стадия), родитель считает долю
и вызывает on_progress; отмена — событие cancel, по нему процесс убивается.
"""
from __future__ import annotations

import ctypes
import multiprocessing as mp
import os
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import cellularity, cellularity_slide as cs
from .config import StorageConfig

MEMORY_LIMIT_MB = 500          # предел памяти процесса расчёта (критерий приёмки этапа 11)
TIME_LIMIT_S = 1800            # предел времени одного расчёта
BASELINE_MB = 150.0            # библиотеки процесса (NumPy, OpenCV, Numba, OpenSlide) — замер 2026-09-22 на сервере
POLL_S = 0.25

_one_at_a_time = threading.Lock()
_waiting = 0
_waiting_lock = threading.Lock()


class JobError(Exception):
    """Расчёт не выполнен: причина — в тексте, годном для показа пользователю."""


class JobCancelled(JobError):
    pass


@dataclass
class JobSpec:
    storage: StorageConfig
    slide_key: str
    base_mpp: float
    resolution: str | float
    fragments: list[cs.Fragment]
    params: cellularity.Params = field(default_factory=cellularity.Params)
    masks_dir: Path | None = None
    memory_limit_mb: float = MEMORY_LIMIT_MB
    time_limit_s: float = TIME_LIMIT_S


@dataclass
class Progress:
    fragment: int
    of: int
    stage: str
    fraction: float                # 0…1 по всему расчёту
    rss_mb: float = 0.0            # память процесса расчёта в начале стадии


def waiting() -> int:
    """Сколько расчётов ждут очереди (для панели: «в очереди N»)."""
    return _waiting


def stage_fraction(fragment: int, of: int, stage: str) -> float:
    """Доля выполненного по всему расчёту в начале стадии stage фрагмента fragment (с 1)."""
    done = 0.0
    for name in cellularity.STAGES:
        if name == stage:
            break
        done += cellularity.STAGE_WEIGHTS[name]
    return ((fragment - 1) + done) / max(of, 1)


def check_memory(spec: JobSpec) -> float:
    """Оценка пика памяти; JobError, если она выше предела, — до запуска процесса."""
    estimate = cs.estimate_peak_mb(spec.fragments, spec.base_mpp, spec.resolution, BASELINE_MB)
    if estimate > spec.memory_limit_mb:
        raise JobError(
            f"Самый крупный фрагмент потребует около {estimate:.0f} МБ памяти при пределе "
            f"{spec.memory_limit_mb:.0f} МБ: выберите более грубое разрешение (2 мкм на точку) "
            f"или разбейте фрагмент контурами")
    return estimate


# ---------- дочерний процесс ----------

def _lower_priority() -> None:
    try:
        if os.name == "nt":
            below_normal = 0x4000
            ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), below_normal)
        else:
            os.nice(10)
    except Exception:
        pass


DATA_LIMIT_FACTOR = 3   # RLIMIT_DATA = предел × 3: это виртуальная память, а не RSS (см. ниже)


def _limit_data_segment(limit_mb: float) -> None:
    """Запасной предел внутри процесса на случай, если наблюдатель за RSS не успеет:
    RLIMIT_DATA (куча и анонимные отображения — то, что выделяет NumPy).

    Он считает виртуальную память, которая заметно больше RSS: освобождённые массивы
    остаются у распределителя, и после первого фрагмента на 17 млн точек второй падал
    с MemoryError при RSS ниже 500 МБ (сервер, 2026-09-23). Поэтому предел втрое больше
    основного; основной — наблюдатель за RSS в родителе."""
    try:
        import resource
        limit = int(limit_mb * DATA_LIMIT_FACTOR * 2 ** 20)
        soft, hard = resource.getrlimit(resource.RLIMIT_DATA)
        if hard != resource.RLIM_INFINITY:
            limit = min(limit, hard)
        resource.setrlimit(resource.RLIMIT_DATA, (limit, hard))
    except Exception:
        pass


def _worker(conn, spec: JobSpec) -> None:
    _lower_priority()
    _limit_data_segment(spec.memory_limit_mb)
    try:
        from .storage import create_storage
        slide = create_storage(spec.storage).open_slide(spec.slide_key)
        try:
            result = cs.run(slide, spec.base_mpp, spec.resolution, spec.fragments, spec.params,
                            masks_dir=spec.masks_dir, log=lambda *_: None,
                            progress=lambda fragment, of, stage: conn.send(
                                ("progress", (fragment, of, stage, rss_mb(os.getpid())[0]))))
        finally:
            slide.close()
        conn.send(("done", result))
    except MemoryError:
        conn.send(("error", "не хватило памяти: фрагмент слишком велик для этого разрешения"))
    except Exception as exc:  # текст уходит в журнал и пользователю
        conn.send(("error", f"{type(exc).__name__}: {exc}"))
    finally:
        conn.close()


# ---------- память чужого процесса ----------

if os.name == "nt":
    import ctypes.wintypes as wt

    class _PMC(ctypes.Structure):
        _fields_ = [("cb", wt.DWORD), ("PageFaultCount", wt.DWORD), ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t), ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t), ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t), ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t)]

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _k32.K32GetProcessMemoryInfo.argtypes = [wt.HANDLE, ctypes.POINTER(_PMC), wt.DWORD]
    _k32.OpenProcess.restype = wt.HANDLE

    def rss_mb(pid: int) -> tuple[float, float]:
        """(текущая, пиковая) резидентная память процесса, МБ; (0, 0), если процесса нет."""
        handle = _k32.OpenProcess(0x1000, False, pid)   # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return 0.0, 0.0
        try:
            pmc = _PMC()
            pmc.cb = ctypes.sizeof(_PMC)
            if not _k32.K32GetProcessMemoryInfo(handle, ctypes.byref(pmc), pmc.cb):
                return 0.0, 0.0
            return pmc.WorkingSetSize / 2 ** 20, pmc.PeakWorkingSetSize / 2 ** 20
        finally:
            _k32.CloseHandle(handle)
else:
    def rss_mb(pid: int) -> tuple[float, float]:
        current = peak = 0.0
        try:
            with open(f"/proc/{pid}/status", encoding="ascii", errors="replace") as f:
                for line in f:
                    if line.startswith("VmRSS:"):
                        current = int(line.split()[1]) / 1024
                    elif line.startswith("VmHWM:"):
                        peak = int(line.split()[1]) / 1024
        except OSError:
            pass
        return current, peak


# ---------- запуск ----------

def run_in_process(spec: JobSpec, on_progress=None, cancel: threading.Event | None = None) -> dict:
    """Расчёт по spec в дочернем процессе; возвращает результат cellularity_slide.run
    с полями peak_rss_mb и elapsed_s. Ждёт очереди, если идёт другой расчёт.

    on_progress(Progress) — ход; cancel — событие отмены (JobCancelled).
    JobError — предел памяти или времени, ошибка в процессе, отказ по оценке памяти.
    """
    global _waiting
    check_memory(spec)
    with _waiting_lock:
        _waiting += 1
    with _one_at_a_time:
        with _waiting_lock:
            _waiting -= 1
        return _run_locked(spec, on_progress, cancel)


def _run_locked(spec: JobSpec, on_progress, cancel) -> dict:
    ctx = mp.get_context("spawn")
    parent_conn, child_conn = ctx.Pipe(duplex=False)
    proc = ctx.Process(target=_worker, args=(child_conn, spec), daemon=True, name="cellularity")
    started = time.monotonic()
    proc.start()
    child_conn.close()
    deadline = started + spec.time_limit_s
    peak = 0.0
    outcome = None

    def kill(reason: str):
        if proc.is_alive():
            proc.kill()
        proc.join(5)
        raise JobError(reason)

    try:
        while outcome is None:
            if parent_conn.poll(POLL_S):
                try:
                    kind, payload = parent_conn.recv()
                except EOFError:
                    break
                if kind == "progress":
                    fragment, of, stage, rss = payload
                    if on_progress:
                        on_progress(Progress(fragment, of, stage, stage_fraction(fragment, of, stage), rss))
                else:
                    outcome = (kind, payload)
                    break
            current, hwm = rss_mb(proc.pid)
            peak = max(peak, current, hwm)
            if cancel is not None and cancel.is_set():
                if proc.is_alive():
                    proc.kill()
                proc.join(5)
                raise JobCancelled("расчёт отменён")
            if current > spec.memory_limit_mb:
                kill(f"расчёт остановлен: память процесса {current:.0f} МБ превысила предел "
                     f"{spec.memory_limit_mb:.0f} МБ — выберите более грубое разрешение")
            if time.monotonic() > deadline:
                kill(f"расчёт остановлен: превышен предел времени {spec.time_limit_s:.0f} с")
            if not proc.is_alive() and not parent_conn.poll():
                break
        if outcome is None:
            proc.join(5)
            code = proc.exitcode
            reason = "не хватило памяти" if code in (-9, 137) else f"код {code}"
            raise JobError(f"процесс расчёта завершился без результата ({reason})")
        kind, payload = outcome
        if kind == "error":
            raise JobError(f"ошибка расчёта: {payload}")
        result = payload
        proc.join(10)
        result["peak_rss_mb"] = round(peak, 1)
        result["elapsed_s"] = round(time.monotonic() - started, 1)
        return result
    finally:
        parent_conn.close()
        if proc.is_alive():
            proc.kill()
            proc.join(5)


if __name__ == "__main__":  # защита для spawn на Windows: модуль импортируется, не выполняется
    sys.exit("Модуль запускается не напрямую: python -m server.manage cellularity <ID>")
