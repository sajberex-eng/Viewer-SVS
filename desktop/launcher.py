"""Запуск настольной программы HemCenterHistoDigital (docs/TOR-desktop.md, раздел 4).

    python -m desktop.launcher        — из исходников, для проверки
    HemCenterHistoDigital.exe         — собранная программа (desktop/app.spec)

Что делает:
1. Не даёт запустить вторую копию: две копии сервера на одной базе SQLite
   недопустимы. Вторая выводит вперёд окно первой и закрывается.
2. Создаёт служебную папку (%LOCALAPPDATA%\\HemCenterHistoDigital) и config.yaml в ней.
3. Поднимает сервер на 127.0.0.1 и свободном порту в фоновом потоке.
4. Открывает окно (WebView2) на адресе входа с одноразовым ключом — ни логина,
   ни пароля пользователь не вводит (НП-2, НП-9).
5. После закрытия окна останавливает сервер; идущий расчёт клеточности
   отменяется, его процесс завершается.
"""
from __future__ import annotations

import ctypes
import logging
import logging.handlers
import multiprocessing
import os
import socket
import sys
import threading
import time
from pathlib import Path

APP_NAME = "HemCenterHistoDigital"
WINDOW_TITLE = APP_NAME
MUTEX_NAME = f"Local\\{APP_NAME}-single-instance"

log = logging.getLogger("desktop")


def service_dir() -> Path:
    """Служебная папка: база, кэш, журнал, настройки окна. Сканов в ней нет (НП-18)."""
    override = os.environ.get("HCHD_HOME")  # для проверок: отдельная папка вместо рабочей
    if override:
        return Path(override)
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / APP_NAME
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_NAME
    return Path.home() / f".{APP_NAME.lower()}"


def ensure_config(home: Path) -> Path:
    """config.yaml программы. Пути абсолютные: в собранной программе папка с кодом —
    это папка установки, и относительный путь увёл бы данные туда (раздел 4)."""
    home.mkdir(parents=True, exist_ok=True)
    path = home / "config.yaml"
    if not path.exists():
        path.write_text(
            "# Настольная программа HemCenterHistoDigital. Сканы открываются из папок,\n"
            "# которые добавлены в каталоге; здесь только база, кэш и журнал.\n"
            "desktop: true\n"
            f"data_dir: {home.as_posix()!r}\n"
            "cache:\n  max_gb: 2.0\n"
            "cellularity:\n  auto: false\n",
            encoding="utf-8",
        )
    return path


# ---------- одна копия программы ----------

_mutex = None


def claim_single_instance() -> bool:
    """Именованный мьютекс Windows: его держит первая копия, пока работает."""
    global _mutex
    if sys.platform != "win32":
        return True
    kernel32 = ctypes.windll.kernel32
    _mutex = kernel32.CreateMutexW(None, False, MUTEX_NAME)
    return kernel32.GetLastError() != 183  # ERROR_ALREADY_EXISTS


def focus_running_window() -> None:
    if sys.platform != "win32":
        return
    user32 = ctypes.windll.user32
    handle = user32.FindWindowW(None, WINDOW_TITLE)
    if handle:
        user32.ShowWindow(handle, 9)  # SW_RESTORE: свёрнутое окно разворачивается
        user32.SetForegroundWindow(handle)


def kill_children_on_exit() -> None:
    """Программа входит в «задание» Windows с флагом «закрыть всё при закрытии»:
    даже если она упадёт, система завершит её дочерние процессы — расчёт
    клеточности и WebView2 — и в диспетчере задач ничего не останется (раздел 10)."""
    if sys.platform != "win32":
        return

    class BasicLimits(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", ctypes.c_uint32), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", ctypes.c_uint32),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", ctypes.c_uint32),
                    ("SchedulingClass", ctypes.c_uint32)]

    class IoCounters(ctypes.Structure):
        _fields_ = [(name, ctypes.c_uint64) for name in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class ExtendedLimits(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", BasicLimits), ("IoInfo", IoCounters),
                    ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

    kernel32 = ctypes.windll.kernel32
    kernel32.CreateJobObjectW.restype = ctypes.c_void_p
    kernel32.GetCurrentProcess.restype = ctypes.c_void_p
    kernel32.SetInformationJobObject.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
    kernel32.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        return
    limits = ExtendedLimits()
    limits.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    ok = kernel32.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits))  # Extended
    if ok and kernel32.AssignProcessToJobObject(job, kernel32.GetCurrentProcess()):
        kill_children_on_exit.job = job  # ручка живёт, пока живёт процесс
    else:
        log.warning("Задание Windows для дочерних процессов не создано")


# ---------- журнал ----------

def setup_logging(home: Path) -> None:
    """Консоли у программы нет — журнал пишется в файл с ротацией."""
    folder = home / "logs"
    folder.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(folder / "app.log", maxBytes=5_000_000, backupCount=3,
                                                   encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(logging.INFO)


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


# ---------- то, что странице даёт программа ----------

class Api:
    """Методы доступны странице как window.pywebview.api.*. Окно хранится в
    закрытом поле: pywebview отдаёт странице только открытые методы."""

    def __init__(self) -> None:
        self._window = None

    def pick_folder(self):
        """Системное окно выбора папки для «Добавить папку» (НП-12)."""
        import webview

        chosen = self._window.create_file_dialog(webview.FileDialog.FOLDER)
        return chosen[0] if chosen else None


def main() -> int:
    multiprocessing.freeze_support()  # дочерний процесс расчёта не должен запускать вторую программу
    if not claim_single_instance():
        focus_running_window()
        return 0

    home = service_dir()
    config = ensure_config(home)
    setup_logging(home)
    kill_children_on_exit()
    os.environ["VIEWER_CONFIG"] = str(config)
    os.environ.setdefault("NUMBA_CACHE_DIR", str(home / "cache" / "numba"))

    import uvicorn
    import webview

    from server.main import app

    port = free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_config=None,
                                           access_log=False, lifespan="on"))
    thread = threading.Thread(target=server.run, name="server", daemon=True)
    thread.start()
    deadline = time.monotonic() + 60
    while not server.started:
        if not thread.is_alive() or time.monotonic() > deadline:
            log.error("Сервер программы не запустился")
            return 1
        time.sleep(0.05)
    log.info("Программа запущена: порт %s, папка %s", port, home)

    api = Api()
    key = app.state.launch_keys.issue()
    window = webview.create_window(
        WINDOW_TITLE, f"http://127.0.0.1:{port}/desktop/enter?key={key}", js_api=api,
        width=1440, height=900, min_size=(900, 600), text_select=True,
    )
    api._window = window
    # Скачивание (CSV, журнал) идёт через окно «Сохранить как» (НП-7)
    webview.settings["ALLOW_DOWNLOADS"] = True
    webview.settings["OPEN_DEVTOOLS_IN_DEBUG"] = False
    # Свой профиль WebView2: настройки страницы (localStorage) живут между запусками,
    # cookie сессии — нет (у неё нет срока), чужой браузер её не видит (НП-10)
    webview.start(private_mode=False, storage_path=str(home / "webview"))

    log.info("Окно закрыто, сервер останавливается")
    server.should_exit = True
    thread.join(timeout=15)
    log.info("Программа завершена")
    return 0


if __name__ == "__main__":
    code = main()
    # Выход без ожидания рабочих потоков: поток расчёта клеточности дожидается конца
    # своего расчёта, и после закрытия окна процесс висел больше минуты — а пока он
    # жив, повторный запуск упирается в «одну копию» и ничего не показывает
    # (найдено 2026-09-26). База к этому моменту записана: каждое изменение
    # фиксируется сразу; незаконченный расчёт следующий запуск помечает неудачным.
    # Дочерние процессы закрывает задание Windows (kill_children_on_exit).
    logging.shutdown()
    os._exit(code)
