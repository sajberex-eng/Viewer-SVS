# Сборка настольной программы PyInstaller (docs/TOR-desktop.md, раздел 9).
#
#   .venv\Scripts\python.exe -m PyInstaller --noconfirm --distpath build\dist --workpath build\work desktop\app.spec
#
# Итог — папка build\dist\HemCenterHistoDigital с HemCenterHistoDigital.exe внутри.
# Установщика нет (решение заказчика 2026-09-26): папку копируют куда удобно и
# запускают exe. Режим «папка», а не «один файл»: с NumPy, OpenCV, Numba и
# OpenSlide программа весит сотни мегабайт, и один файл распаковывал бы их во
# временную папку при каждом запуске (НП-25).
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_submodules

ROOT = Path(SPECPATH).parent

datas = [(str(ROOT / "web"), "web")]
binaries = []
hiddenimports = collect_submodules("server") + collect_submodules("desktop")
# Пакеты с библиотеками и модулями, которые подключаются по имени во время работы
for package in ("openslide", "openslide_bin", "webview", "uvicorn", "numba", "llvmlite", "anthropic"):
    package_datas, package_binaries, package_hidden = collect_all(package)
    datas += package_datas
    binaries += package_binaries
    hiddenimports += package_hidden

a = Analysis(
    [str(ROOT / "desktop" / "launcher.py")],
    pathex=[str(ROOT)],
    datas=datas,
    binaries=binaries,
    hiddenimports=hiddenimports,
    excludes=["tkinter", "matplotlib", "IPython", "pytest", "selenium"],
    # Numba кэширует скомпилированные функции только при исходном .py рядом —
    # иначе каждый расчёт клеточности компилировал бы их заново (раздел 8)
    module_collection_mode={"server": "py"},
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="HemCenterHistoDigital",
    icon=str(ROOT / "desktop" / "icon.ico") if sys.platform == "win32" else None,  # у macOS — icns в BUNDLE
    console=False,   # окно программы без чёрного окна консоли
    upx=False,       # сжатие UPX антивирусы принимают за признак вредоносной программы
)

coll = COLLECT(exe, a.binaries, a.datas, name="HemCenterHistoDigital", upx=False)

# macOS: .app рядом с папкой. Подписи разработчика нет (решение заказчика) — PyInstaller
# сам ставит ad-hoc подпись, без неё бинарник arm64 вообще не запустится. Первый запуск
# разрешается вручную: «Системные настройки → Конфиденциальность и безопасность».
# Версия — из переменной HCHD_VERSION (workflow берёт её из тега).
if sys.platform == "darwin":
    import os

    version = os.environ.get("HCHD_VERSION", "1.0")
    app = BUNDLE(
        coll,
        name="HemCenterHistoDigital.app",
        icon=str(ROOT / "desktop" / "icon.icns"),
        bundle_identifier="kz.hemcenter.histodigital",  # тот же BUNDLE_ID в launcher.py
        version=version,
        info_plist={
            "CFBundleName": "HemCenterHistoDigital",
            "CFBundleDisplayName": "HemCenterHistoDigital",
            "CFBundleShortVersionString": version,
            "NSHighResolutionCapable": True,
            "LSMinimumSystemVersion": "12.0",
            "NSAppTransportSecurity": {"NSAllowsLocalNetworking": True},
        },
    )
