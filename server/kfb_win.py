"""Чтение сканов KFBio (.kfb) в Windows — настольная программа (docs/TOR-desktop.md, раздел 7).

Библиотека KFBio `ImageOperationLib.dll` (64 бит) берётся из установленной
программы KFSlideOS и в нашу сборку не входит (НП-20). Заголовочного файла у
библиотеки нет; объявления функций проверены на настоящем скане 2026-09-26:

    InitImageFileFunc(ImageInfo*, const char* path) -> bool      ImageInfo — один указатель, 8 байт
    UnInitImageFileFunc(ImageInfo*) -> bool
    GetHeaderInfoFunc(ImageInfo, int* h, int* w, int* scale, float* spend_time,
                      double* scan_time, float* mpp, int* block) -> bool
    GetImageDataRoiFunc(ImageInfo, float scale, int x, int y, int w, int h,
                        uchar** data, int* length, bool jpeg) -> bool
        scale — увеличение уровня (40 — полное при скане 40×), x, y — в точках
        этого уровня; jpeg=False даёт сырые точки RGB ровно w×h×3; область,
        целиком за краем, не читается
    GetLableInfoFunc(ImageInfo, uchar** jpeg, int* length, int* w, int* h) -> bool
    DeleteImageDataFunc(void*) -> bool      каждый вызов выделяет новый буфер;
                                            освобождать его безопасно (в Linux-ASlide
                                            второе чтение этикетки роняло процесс — здесь нет)

Путь передаётся байтами в кодировке ANSI; чтобы кириллица и другие символы в
имени не мешали, берётся короткое имя файла 8.3.
"""
from __future__ import annotations

import ctypes
import io
import os
import threading
from ctypes import POINTER, byref, c_bool, c_char_p, c_double, c_float, c_int, c_ubyte, c_void_p
from pathlib import Path

from PIL import Image

LIBRARY = "ImageOperationLib.dll"
MIN_LEVEL_SIDE = 512      # уровни пирамиды: вдвое меньше, пока большая сторона не меньше этого
READ_CHUNK = 4096         # крупные области читаются кусками не больше 4096 × 4096
LABEL = "label"           # имя как у OpenSlide; обзорное фото стекла (macro) не отдаётся никому


def _candidates() -> list[Path]:
    """Где искать библиотеку: папка из VIEWER_KFB_DIR, затем установленная KFSlideOS."""
    found = []
    if os.environ.get("VIEWER_KFB_DIR"):
        found.append(Path(os.environ["VIEWER_KFB_DIR"]))
    roots = [Path(os.environ["LOCALAPPDATA"]) / "Programs"] if os.environ.get("LOCALAPPDATA") else []
    roots += [Path(os.environ[name]) for name in ("ProgramFiles", "ProgramFiles(x86)") if os.environ.get(name)]
    found += [root / "KFSlideOS" / "resources" / "server" / "backend" for root in roots]
    return found


def library_dir() -> Path | None:
    return next((folder for folder in _candidates() if (folder / LIBRARY).is_file()), None)


AVAILABLE = library_dir() is not None
_lib = None
_load_lock = threading.Lock()


def unavailable_reason() -> str:
    return "" if AVAILABLE else "для KFB нужна установленная программа KFSlideOS (библиотека KFBio не найдена)"


def _library():
    """Загрузка при первом скане KFB: веб-сервису и сканам других форматов она не нужна.
    Своя папка библиотеки добавляется в поиск: рядом лежат её OpenCV, libcurl и прочее
    (НП-21); наши OpenCV и OpenSlide называются иначе и уже загружены."""
    global _lib
    with _load_lock:
        if _lib is not None:
            return _lib
        folder = library_dir()
        if folder is None:
            raise OSError(unavailable_reason())
        os.add_dll_directory(str(folder))
        lib = ctypes.CDLL(str(folder / LIBRARY))
        lib.InitImageFileFunc.restype = c_bool
        lib.InitImageFileFunc.argtypes = [c_void_p, c_char_p]
        lib.UnInitImageFileFunc.restype = c_bool
        lib.UnInitImageFileFunc.argtypes = [c_void_p]
        lib.GetHeaderInfoFunc.restype = c_bool
        lib.GetHeaderInfoFunc.argtypes = [c_void_p, POINTER(c_int), POINTER(c_int), POINTER(c_int), POINTER(c_float),
                                          POINTER(c_double), POINTER(c_float), POINTER(c_int)]
        lib.GetImageDataRoiFunc.restype = c_bool
        lib.GetImageDataRoiFunc.argtypes = [c_void_p, c_float, c_int, c_int, c_int, c_int,
                                            POINTER(POINTER(c_ubyte)), POINTER(c_int), c_bool]
        lib.GetLableInfoFunc.restype = c_bool
        lib.GetLableInfoFunc.argtypes = [c_void_p, POINTER(POINTER(c_ubyte)), POINTER(c_int), POINTER(c_int),
                                         POINTER(c_int)]
        lib.DeleteImageDataFunc.restype = c_bool
        lib.DeleteImageDataFunc.argtypes = [c_void_p]
        _lib = lib
        return lib


def _ansi_path(path: str) -> bytes:
    buffer = ctypes.create_unicode_buffer(32768)
    if ctypes.windll.kernel32.GetShortPathNameW(str(path), buffer, len(buffer)):
        path = buffer.value
    try:
        return path.encode("mbcs", errors="strict")
    except UnicodeEncodeError as exc:
        raise OSError("имя файла или папки KFB содержит символы, которые библиотека KFBio не прочитает; "
                      "переименуйте их латиницей или кириллицей") from exc


class _AssociatedImages:
    """Этикетка стекла; чтение под замком скана."""

    def __init__(self, owner: "KfbFile"):
        self._owner = owner
        self._names = (LABEL,) if owner._read_label(probe=True) else ()

    def __contains__(self, name) -> bool:
        return name in self._names

    def __iter__(self):
        return iter(self._names)

    def __getitem__(self, name) -> Image.Image:
        if name not in self._names:
            raise KeyError(name)
        return self._owner._read_label()


class KfbFile:
    """Скан KFB с интерфейсом, которым пользуется сервис (как у openslide.OpenSlide).
    Все обращения к файлу — под замком: неизвестно, выдерживает ли библиотека
    одновременное чтение из нескольких потоков."""

    def __init__(self, path: str):
        self._lib = _library()
        self._lock = threading.Lock()
        self._info = (c_ubyte * 64)()   # ImageInfo — 8 байт; запас на случай другой версии библиотеки
        if not self._lib.InitImageFileFunc(ctypes.addressof(self._info), _ansi_path(path)):
            raise OSError("библиотека KFBio не открыла файл: он повреждён или это не скан KFB")
        self._handle = c_void_p.from_buffer(self._info).value
        height, width, scale, block = c_int(), c_int(), c_int(), c_int()
        spend, mpp, scan_time = c_float(), c_float(), c_double()
        if not self._lib.GetHeaderInfoFunc(self._handle, byref(height), byref(width), byref(scale), byref(spend),
                                           byref(scan_time), byref(mpp), byref(block)) or width.value <= 0:
            self._release()
            raise OSError("библиотека KFBio не прочитала заголовок скана")
        self.scan_scale = float(scale.value or 40)
        self.dimensions = (width.value, height.value)
        downsamples = [1.0]
        while max(self.dimensions) / downsamples[-1] / 2 >= MIN_LEVEL_SIDE:
            downsamples.append(downsamples[-1] * 2)
        self.level_downsamples = tuple(downsamples)
        self.level_dimensions = tuple(
            (max(1, int(width.value // ds)), max(1, int(height.value // ds))) for ds in downsamples)
        self.level_count = len(downsamples)
        self.properties = {"openslide.vendor": "kfbio", "openslide.objective-power": str(scale.value)}
        if mpp.value > 0:
            self.properties["openslide.mpp-x"] = self.properties["openslide.mpp-y"] = f"{mpp.value:.6f}"
        self.associated_images = _AssociatedImages(self)

    # ---------- чтение ----------

    def _roi(self, level: int, x: int, y: int, w: int, h: int) -> bytes:
        data = POINTER(c_ubyte)()
        length = c_int()
        scale = c_float(self.scan_scale / self.level_downsamples[level])
        ok = self._lib.GetImageDataRoiFunc(self._handle, scale, x, y, w, h, byref(data), byref(length), False)
        address = ctypes.cast(data, c_void_p).value
        try:
            if not ok or not address or length.value != w * h * 3:
                raise OSError(f"библиотека KFBio не прочитала область {w}×{h} на уровне {level}")
            return ctypes.string_at(address, length.value)
        finally:
            if address:
                self._lib.DeleteImageDataFunc(address)

    def get_best_level_for_downsample(self, downsample: float) -> int:
        best = 0
        for level, ds in enumerate(self.level_downsamples):
            if ds <= downsample:
                best = level
        return best

    def read_region(self, location, level, size) -> Image.Image:
        """Как у OpenSlide: location — в точках уровня 0, size — в точках уровня;
        всё, что за краем скана, прозрачное."""
        ds = self.level_downsamples[level]
        lw, lh = self.level_dimensions[level]
        x0, y0 = int(location[0] // ds), int(location[1] // ds)
        width, height = int(size[0]), int(size[1])
        result = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        left, top = max(x0, 0), max(y0, 0)
        right, bottom = min(x0 + width, lw), min(y0 + height, lh)
        with self._lock:
            for cy in range(top, bottom, READ_CHUNK):
                for cx in range(left, right, READ_CHUNK):
                    cw, ch = min(READ_CHUNK, right - cx), min(READ_CHUNK, bottom - cy)
                    piece = Image.frombytes("RGB", (cw, ch), self._roi(level, cx, cy, cw, ch))
                    result.paste(piece, (cx - x0, cy - y0))
        return result

    def get_thumbnail(self, size) -> Image.Image:
        level = self.level_count - 1
        region = self.read_region((0, 0), level, self.level_dimensions[level])
        image = Image.new("RGB", region.size, "white")
        image.paste(region, mask=region.getchannel("A"))
        image.thumbnail(size, Image.Resampling.LANCZOS)
        return image

    def _read_label(self, probe: bool = False):
        data = POINTER(c_ubyte)()
        length, w, h = c_int(), c_int(), c_int()
        with self._lock:
            ok = self._lib.GetLableInfoFunc(self._handle, byref(data), byref(length), byref(w), byref(h))
            address = ctypes.cast(data, c_void_p).value
            try:
                raw = ctypes.string_at(address, length.value) if ok and address and length.value > 0 else b""
            finally:
                if address:
                    self._lib.DeleteImageDataFunc(address)
        if probe:
            return bool(raw)
        if not raw:
            raise KeyError(LABEL)
        image = Image.open(io.BytesIO(raw))
        image.load()
        return image

    # ---------- закрытие ----------

    def _release(self) -> None:
        self._lib.UnInitImageFileFunc(ctypes.addressof(self._info))
        self._handle = None

    def close(self) -> None:
        with self._lock:
            if self._handle is not None:
                self._release()

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        self.close()
