"""Запуск настольной программы HemCenterHistoDigital (docs/TOR-desktop.md, раздел 4).

    python -m desktop.launcher        — из исходников, для проверки
    HemCenterHistoDigital.exe         — собранная программа (desktop/app.spec)

Что делает:
1. Не даёт запустить вторую копию: две копии сервера на одной базе SQLite
   недопустимы. Вторая выводит вперёд окно первой и закрывается.
2. Создаёт служебную папку (%LOCALAPPDATA%\\HemCenterHistoDigital) и config.yaml в ней.
3. Поднимает сервер на 127.0.0.1 и свободном порту в фоновом потоке.
4. Открывает окно (WebView2, на macOS WKWebView) на адресе входа с одноразовым
   ключом — ни логина, ни пароля пользователь не вводит (НП-2, НП-9). Системное окно
   выбора папки и полный экран страница просит у окна через сервер (WindowHooks).
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
BUNDLE_ID = "kz.hemcenter.histodigital"  # тот же, что в desktop/app.spec (macOS)

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


# ---------- файлы из скачанного архива ----------

def unblock_downloaded_files(base: Path | None = None) -> int:
    """Снимает с файлов программы пометку «получено из интернета» (поток Zone.Identifier).

    Браузер ставит её на zip, а проводник переносит на каждый распакованный файл. Для DLL с
    такой пометкой .NET отказывается загружаться: pythonnet не находит Python.Runtime.Loader.Initialize,
    и окно не открывается («Unhandled exception in script»). Найдено у заказчика 2026-10-23 на
    выпуске desktop-v1.0, на CI и у разработчика файлы не помечены, поэтому там всё работало.
    Возвращает, со скольких файлов пометка снята."""
    if sys.platform != "win32":
        return 0
    if base is None:
        if not getattr(sys, "frozen", False):
            return 0
        base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    marker = base / "pythonnet" / "runtime" / "Python.Runtime.dll"
    if not os.path.exists(f"{marker}:Zone.Identifier"):  # файлы не помечены — обход не нужен
        return 0
    cleared = 0
    for folder, _dirs, files in os.walk(base):
        for name in files:
            try:
                os.remove(os.path.join(folder, name) + ":Zone.Identifier")
                cleared += 1
            except OSError:  # у файла пометки нет или папка только для чтения
                pass
    return cleared


# ---------- сообщение об ошибке ----------

def show_error(message: str, log_path: Path) -> None:
    """Системное окно с текстом ошибки: у программы нет консоли, и без него пользователь
    видит либо трассу (Windows, «Unhandled exception in script»), либо ничего (macOS).
    Найдено на выпуске 1.0 2026-10-23."""
    hint = ""
    if sys.platform == "win32":
        hint = ("Если архив скачан из интернета: закройте программу, удалите распакованную папку, "
                "в свойствах zip-файла поставьте «Разблокировать», распакуйте заново.\n\n")
    text = f"{message}\n\n{hint}Подробности: {log_path}"
    try:
        if sys.platform == "win32":
            ctypes.windll.user32.MessageBoxW(None, text, WINDOW_TITLE, 0x10)  # MB_ICONERROR
        elif sys.platform == "darwin":
            import subprocess

            # Перевод строки в литерале AppleScript ненадёжен — склеиваем через return
            body = text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", '" & return & "')
            script = (f'display dialog "{body}" with title "{WINDOW_TITLE}" buttons {{"OK"}} '
                      'default button 1 with icon stop giving up after 600')
            subprocess.run(["osascript", "-e", script], timeout=600, check=False,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        log.exception("Окно с ошибкой не показано")


# ---------- одна копия программы ----------

_mutex = None
_lock_file = None


def claim_single_instance() -> bool:
    """Первая копия держит замок, пока работает. Windows — именованный мьютекс;
    macOS и прочие — файл-замок в служебной папке (flock снимается сам, когда
    процесс умирает, в том числе упав)."""
    global _mutex, _lock_file
    if sys.platform == "win32":
        kernel32 = ctypes.windll.kernel32
        _mutex = kernel32.CreateMutexW(None, False, MUTEX_NAME)
        return kernel32.GetLastError() != 183  # ERROR_ALREADY_EXISTS
    import fcntl

    home = service_dir()
    home.mkdir(parents=True, exist_ok=True)
    _lock_file = open(home / "instance.lock", "w")
    try:
        fcntl.flock(_lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def focus_running_window() -> None:
    if sys.platform == "darwin":
        try:
            from AppKit import NSApplicationActivateIgnoringOtherApps, NSRunningApplication

            for running in NSRunningApplication.runningApplicationsWithBundleIdentifier_(BUNDLE_ID):
                running.activateWithOptions_(NSApplicationActivateIgnoringOtherApps)
        except Exception:  # без AppKit (запуск из исходников без pyobjc) просто выходим
            pass
        return
    if sys.platform != "win32":
        return
    user32 = ctypes.windll.user32
    handle = user32.FindWindowW(None, WINDOW_TITLE)
    if handle:
        user32.ShowWindow(handle, 9)  # SW_RESTORE: свёрнутое окно разворачивается
        user32.SetForegroundWindow(handle)


def stop_child_processes() -> None:
    """Выход идёт через os._exit без ожидания, поэтому процесс расчёта клеточности
    (spawn-потомок) сам не завершится. Windows закрыло бы его заданием
    (kill_children_on_exit), macOS — нет: завершаем явно. Окно и WebView/WebKit
    система закрывает вместе с программой."""
    for child in multiprocessing.active_children():
        try:
            child.kill()
        except Exception:
            pass


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


# ---------- то, что странице даёт окно программы ----------

class WindowHooks:
    """Страница просит окно через сервер (маршруты /api/desktop/* в server/main.py),
    а не через мост window.pywebview.api: мост строится через new Function, и WKWebView
    на macOS резал его строгим CSP (выпуск 1.0.2), а с ослабленным CSP у заказчика всё равно
    открывался ввод пути вручную (1.0.4). Сервер работает в том же процессе, что и окно,
    поэтому обработчик просто вызывает pywebview; тот сам переводит вызов в главный поток."""

    def __init__(self, window) -> None:
        self._window = window

    def pick_folder(self) -> str | None:
        """Системное окно выбора папки для «Добавить папку» (НП-12). None — выбор отменён."""
        import webview

        chosen = self._window.create_file_dialog(webview.FileDialog.FOLDER)
        return chosen[0] if chosen else None

    def toggle_fullscreen(self) -> None:
        """Полный экран самим окном: в WKWebView (macOS) Fullscreen API страницы выключен,
        и кнопка «Во весь экран» там ничего не делала. На macOS — родное toggleFullScreen
        NSWindow (своё состояние окно ведёт само, повторный вызов сворачивает обратно)."""
        if sys.platform == "darwin":
            from PyObjCTools import AppHelper

            native = self._window.native
            AppHelper.callAfter(lambda: native.toggleFullScreen_(None))
        else:
            self._window.toggle_fullscreen()


def tune_macos_window(window) -> None:
    """Что pywebview на macOS не включает сам (замечания заказчика 2026-10-24):
    жесты тачпада «назад/вперёд» (allowsBackForwardNavigationGestures у WKWebView) и
    разрешение окну разворачиваться во весь экран (NSWindowCollectionBehaviorFullScreenPrimary).
    WKWebView становится contentView окна только после первой загрузки страницы, поэтому
    жесты включаются по событию loaded."""
    if sys.platform != "darwin":
        return
    from PyObjCTools import AppHelper

    def allow_fullscreen() -> None:
        try:
            native = window.native
            native.setCollectionBehavior_(native.collectionBehavior() | (1 << 7))  # FullScreenPrimary
        except Exception:
            log.exception("Полный экран окну не разрешён")

    def enable_gestures() -> None:
        try:
            view = window.native.contentView()
            if view.respondsToSelector_("setAllowsBackForwardNavigationGestures:"):
                view.setAllowsBackForwardNavigationGestures_(True)
                log.info("Жесты тачпада «назад» и «вперёд» включены")
            else:
                log.warning("Жесты тачпада не включены: contentView окна — %s", type(view).__name__)
        except Exception:
            log.exception("Жесты тачпада не включены")

    def on_shown() -> None:
        AppHelper.callAfter(allow_fullscreen)

    def on_loaded() -> None:
        window.events.loaded -= on_loaded  # один раз: страницы потом меняются, окно — нет
        AppHelper.callAfter(enable_gestures)

    window.events.shown += on_shown
    window.events.loaded += on_loaded


def main() -> int:
    multiprocessing.freeze_support()  # дочерний процесс расчёта не должен запускать вторую программу
    unblocked = unblock_downloaded_files()  # до импорта окна: иначе pythonnet не загрузится
    if len(sys.argv) == 4 and sys.argv[1] == "--selftest":  # самопроверка сборки на CI, без окна
        from desktop.selftest import run

        try:
            return run(Path(sys.argv[2]), Path(sys.argv[3]))
        except BaseException:  # окно ошибки безоконной сборки на CI некому закрыть — пишем в отчёт
            import traceback

            with open(sys.argv[3], "a", encoding="utf-8") as handle:
                handle.write("[СБОЙ] исключение самопроверки:\n" + traceback.format_exc())
            return 1
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
            show_error("Программа не запустилась: внутренний сервер не ответил.", home / "logs" / "app.log")
            return 1
        time.sleep(0.05)
    log.info("Программа запущена: порт %s, папка %s", port, home)
    if unblocked:
        log.info("С файлов программы снята пометка «получено из интернета»: %s", unblocked)

    key = app.state.launch_keys.issue()
    window = webview.create_window(
        WINDOW_TITLE, f"http://127.0.0.1:{port}/desktop/enter?key={key}",
        width=1440, height=900, min_size=(900, 600), text_select=True,
    )
    app.state.desktop_window = WindowHooks(window)
    tune_macos_window(window)
    # Скачивание (CSV, журнал) идёт через окно «Сохранить как» (НП-7)
    webview.settings["ALLOW_DOWNLOADS"] = True
    webview.settings["OPEN_DEVTOOLS_IN_DEBUG"] = False
    # Свой профиль WebView2: настройки страницы (localStorage) живут между запусками,
    # cookie сессии — нет (у неё нет срока), чужой браузер её не видит (НП-10)
    try:
        webview.start(private_mode=False, storage_path=str(home / "webview"))
    except Exception:
        # Без окна программе нечего показывать. Безоконная сборка в таком случае молча
        # показывала «Unhandled exception in script» с трассой, понятной только разработчику
        log.exception("Окно программы не открылось")
        show_error("Не удалось открыть окно программы.", home / "logs" / "app.log")
        server.should_exit = True
        return 1

    log.info("Окно закрыто, сервер останавливается")
    server.should_exit = True
    thread.join(timeout=15)
    log.info("Программа завершена")
    return 0


if __name__ == "__main__":
    code = main()
    selftest_report = Path(sys.argv[3]) if len(sys.argv) == 4 and sys.argv[1] == "--selftest" else None
    if selftest_report:
        with open(selftest_report, "a", encoding="utf-8") as handle:
            handle.write(f"main завершён, код {code}\n")
    stop_child_processes()
    if selftest_report:
        with open(selftest_report, "a", encoding="utf-8") as handle:
            handle.write("дочерние остановлены, выход\n")
    # Выход без ожидания рабочих потоков: поток расчёта клеточности дожидается конца
    # своего расчёта, и после закрытия окна процесс висел больше минуты — а пока он
    # жив, повторный запуск упирается в «одну копию» и ничего не показывает
    # (найдено 2026-09-26). База к этому моменту записана: каждое изменение
    # фиксируется сразу; незаконченный расчёт следующий запуск помечает неудачным.
    # Дочерние процессы закрывает задание Windows (kill_children_on_exit).
    logging.shutdown()
    os._exit(code)
