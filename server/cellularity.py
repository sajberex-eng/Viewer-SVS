"""Оценка клеточности костного мозга по MarrowQuant 2.0 (этап 11, раздел 13.8 ТЗ).

Перенос класса MarrowQuant из MarrowQuant2.0.groovy (github.com/Naveiras-Lab/
MarrowQuant2.0, лицензия BSD-3) с ImageJ на NumPy и OpenCV. Каждый шаг повторяет
вызов ImageJ, которым он сделан в оригинале, вместе с его неочевидными правилами
(ссылки на исходники ImageJ 1.54f — в комментариях). Сверка по точкам — с эталоном
`tools/mq_reference/MQReference.java`, который выполняет те же шаги в самом ImageJ.

Что важно знать о поведении оригинала (всё воспроизводится):

- пороги по гистограмме 32-битных изображений строятся в диапазоне всей области
  (прямоугольника вокруг фрагмента), а сама гистограмма — только по ткани;
- у изображения дисперсии кость и всё вне ткани закрашены Float.MAX_VALUE до расчёта
  порога: они попадают в последнюю ячейку гистограммы и влияют на порог стромы;
- «Options... iterations=50 count=5 pad do=Dilate» у жировой маски идёт без «black»,
  а значит сбрасывает глобальную настройку «чёрный фон»: объектом становятся точки
  0, и шаг на деле сужает кандидатов в жировые клетки, а не расширяет их;
- размер частиц задаётся в мкм² и переводится в точки, а округлость (circularity)
  считается по периметру в точках — у ImageJ контур частицы без калибровки.

Память (КЛ-7): область целиком в памяти не держится. RGB приходит строками по требованию
(см. SlideRows в cellularity_slide), всё с плавающей точкой и гистограммы считаются полосами
строк, двоичная морфология — полосами с полем в число итераций, разделение слипшихся частиц —
по связным частям. Целиком лежат только однобайтовые слои области (см. PEAK_BYTES_PER_PIXEL).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import cv2
import numpy as np

from . import ij_watershed

FLOAT_MAX = np.float32(3.4028235e38)
STRIP = 256  # строк в полосе: вычисления с плавающей точкой идут полосами ради памяти
STRIP_PIXELS = 1_000_000  # но не больше точек в полосе: временные float64 растут с шириной области


def strip_rows(w: int) -> int:
    """Высота полосы для области шириной w: STRIP строк, но не больше STRIP_PIXELS точек.
    Разложение окрасок держит на полосу около восьми массивов float64 — при ширине 8000
    точек полоса в 256 строк стоила бы 130 МБ, с пределом по точкам — 64 МБ."""
    return max(16, min(STRIP, STRIP_PIXELS // max(w, 1)))


def _strips(h: int, halo: int = 0, rows: int | None = None, w: int | None = None):
    """Полосы строк [y0, y1) и их расширение на halo строк для фильтров 3×3."""
    rows = rows or (strip_rows(w) if w else STRIP)
    for y0 in range(0, h, rows):
        y1 = min(y0 + rows, h)
        yield y0, y1, max(y0 - halo, 0), min(y1 + halo, h)


@dataclass
class Params:
    adip_min_um2: float = 300.0          # как в MarrowQuant2.0.groovy
    adip_max_um2: float = 1e9            # в оригинале предела нет (КЛ-5 — задать)
    min_circularity: float = 0.3
    min_roundness: float = 0.36          # «Hard coded by Ibrahim Berki»


@dataclass
class Result:
    bone: np.ndarray
    imv: np.ndarray
    hemato: np.ndarray
    adip: np.ndarray
    n_adip: int
    debug: dict = field(default_factory=dict)


# ---------- разложение окрасок: Colour Deconvolution 3.0.2, «H&E DAB» ----------

_HE_DAB = ((0.650, 0.704, 0.286), (0.072, 0.990, 0.105), (0.268, 0.570, 0.776))


def _deconvolution_q() -> list[float]:
    """Матрица q ровно так, как её считает StainMatrix.compute (с заменой нулей на 0,001)."""
    cosx, cosy, cosz = [0.0] * 3, [0.0] * 3, [0.0] * 3
    for i, (r, g, b) in enumerate(_HE_DAB):
        n = math.sqrt(r * r + g * g + b * b)
        cosx[i], cosy[i], cosz[i] = r / n, g / n, b / n
    n = math.sqrt(cosx[2] ** 2 + cosy[2] ** 2 + cosz[2] ** 2)
    cosx[2], cosy[2], cosz[2] = cosx[2] / n, cosy[2] / n, cosz[2] / n
    for i in range(3):
        cosx[i] = cosx[i] or 0.001
        cosy[i] = cosy[i] or 0.001
        cosz[i] = cosz[i] or 0.001
    A = cosy[1] - cosx[1] * cosy[0] / cosx[0]
    V = cosz[1] - cosx[1] * cosz[0] / cosx[0]
    C = cosz[2] - cosy[2] * V / A + cosx[2] * (V / A * cosy[0] / cosx[0] - cosz[0] / cosx[0])
    q = [0.0] * 9
    q[2] = (-cosx[2] / cosx[0] - cosx[2] / A * cosx[1] / cosx[0] * cosy[0] / cosx[0] + cosy[2] / A * cosx[1] / cosx[0]) / C
    q[1] = -q[2] * V / A - cosx[1] / (cosx[0] * A)
    q[0] = 1.0 / cosx[0] - q[1] * cosy[0] / cosx[0] - q[2] * cosz[0] / cosx[0]
    q[5] = (-cosy[2] / A + cosx[2] / A * cosy[0] / cosx[0]) / C
    q[4] = -q[5] * V / A + 1.0 / A
    q[3] = -q[4] * cosy[0] / cosx[0] - q[5] * cosz[0] / cosx[0]
    q[8] = 1.0 / C
    q[7] = -q[8] * V / A
    q[6] = -q[7] * cosy[0] / cosx[0] - q[8] * cosz[0] / cosx[0]
    return q


_Q = _deconvolution_q()
_LOG255 = math.log(255.0)
_OD = -(255.0 * np.log((np.arange(256, dtype=np.float64) + 1) / 255.0)) / _LOG255


def deconvolve(rgb: np.ndarray) -> list[np.ndarray]:
    """Три 8-битных изображения окрасок (0 — много краски, 255 — нет), как в плагине."""
    r, g, b = (_OD[rgb[..., i]] for i in range(3))
    out = []
    for i in range(3):
        s = r * _Q[i * 3] + g * _Q[i * 3 + 1] + b * _Q[i * 3 + 2]
        v = np.exp(-(s - 255.0) * _LOG255 / 255.0)
        v = np.minimum(v, 255.0)
        # (byte)(0xff & (int)floor(v + 0.5)): отрицательных нет, 255 не превышается
        out.append(np.floor(v + 0.5).astype(np.uint8))
    return out


def sub8(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """ImageCalculator «Subtract» для 8 бит: отрицательное обрезается в 0."""
    return cv2.subtract(a, b)


# ---------- пороги ImageJ (AutoThresholder, ImageProcessor.setAutoThreshold) ----------

def _bilevel(h: np.ndarray) -> int:
    nz = np.flatnonzero(h)
    if len(nz) == 1:
        return int(nz[0]) - 1
    if len(nz) == 2:
        return int(nz[1]) - 1
    return -1


def max_entropy(data: np.ndarray) -> int:
    data = data.astype(np.int64)
    total = data.sum()
    norm = data / total
    P1 = np.cumsum(norm)
    P2 = 1.0 - P1
    eps = 2.220446049250313e-16
    first = next((i for i in range(len(data)) if not abs(P1[i]) < eps), 0)
    last = len(data) - 1
    for i in range(len(data) - 1, first - 1, -1):
        if not abs(P2[i]) < eps:
            last = i
            break
    nzmask = data != 0
    best, threshold = np.finfo(float).tiny, -1   # Double.MIN_VALUE
    for it in range(first, last + 1):
        back = norm[: it + 1][nzmask[: it + 1]] / P1[it]
        ent_back = -(back * np.log(back)).sum()
        obj = norm[it + 1:][nzmask[it + 1:]]
        ent_obj = -((obj / P2[it]) * np.log(obj / P2[it])).sum() if obj.size else 0.0
        if best < ent_back + ent_obj:
            best, threshold = ent_back + ent_obj, it
    return threshold


def _ij_isodata(data: list[int]) -> int:
    max_value = len(data) - 1
    count0, count_max = data[0], data[max_value]
    data[0] = 0
    data[max_value] = 0
    lo = 0
    while data[lo] == 0 and lo < max_value:
        lo += 1
    hi = max_value
    while data[hi] == 0 and hi > 0:
        hi -= 1
    if lo >= hi:
        data[0], data[max_value] = count0, count_max
        return len(data) // 2
    moving = lo
    inc = max(hi // 40, 1)  # noqa: F841 — в ImageJ вычисляется и не используется
    while True:
        s1 = sum(i * data[i] for i in range(lo, moving + 1))
        s2 = sum(data[i] for i in range(lo, moving + 1))
        s3 = sum(i * data[i] for i in range(moving + 1, hi + 1))
        s4 = sum(data[i] for i in range(moving + 1, hi + 1))
        result = (s1 / s2 + s3 / s4) / 2.0
        moving += 1
        if not ((moving + 1) <= result and moving < hi - 1):
            break
    data[0], data[max_value] = count0, count_max
    return int(math.floor(result + 0.5))  # Math.round


def default_isodata(hist: np.ndarray) -> int:
    data = [int(v) for v in hist]
    mode = max(range(len(data)), key=lambda i: (data[i], -i))
    max_count = data[mode]
    max_count2 = max((v for i, v in enumerate(data) if i != mode), default=0)
    if max_count > max_count2 * 2 and max_count2 != 0:
        data[mode] = int(max_count2 * 1.5)
    return _ij_isodata(data)


def auto_threshold(hist: np.ndarray, method: str) -> int:
    t = _bilevel(hist)
    if t >= 0:
        return t
    t = max_entropy(hist) if method == "MaxEntropy" else default_isodata(hist)
    return 0 if t == -1 else t


def to_byte(values: np.ndarray, vmin: float, vmax: float) -> np.ndarray:
    """FloatProcessor.create8BitImage: (v − min)·255/(max − min) + 0,5, обрезка 0…255."""
    scale = 255.0 / (vmax - vmin)
    v = np.maximum(values.astype(np.float64) - vmin, 0.0)
    # (int) у огромного значения в Java даёт Integer.MAX_VALUE, то есть та же 255
    return np.minimum(v * scale + 0.5, 255.0).astype(np.int64)


# ---------- двоичная морфология ImageJ (ByteProcessor.filter, ERODE и DILATE) ----------

_K8 = np.array([[1, 1, 1], [1, 0, 1], [1, 1, 1]], np.float32)


def _neighbours(mask: np.ndarray, outside: int) -> np.ndarray:
    """Число соседей-«объектов» из восьми; за краем картинки — значение outside (0 или 1)."""
    padded = cv2.copyMakeBorder(mask, 1, 1, 1, 1, cv2.BORDER_CONSTANT, value=outside)
    return cv2.filter2D(padded, cv2.CV_8U, _K8, borderType=cv2.BORDER_CONSTANT)[1:-1, 1:-1]


def _iterate_bands(fg: np.ndarray, iterations: int, one_step) -> np.ndarray:
    """Итерации фильтра 3×3 полосами строк с полем в iterations строк.

    Каждая итерация смотрит на одну точку вокруг, поэтому за iterations итераций край
    полосы влияет не дальше чем на iterations строк, и с таким полем результат тот же,
    что у расчёта целиком; временные массивы при этом размером с полосу, а не с область.
    Левый и правый края полосы — настоящие края картинки; верх и низ — тоже, если полоса
    первая или последняя, иначе поле их скрывает.
    """
    fg = np.asarray(fg, np.uint8)
    out = np.empty_like(fg)
    # полоса не короче 10 полей, иначе поле съедает время: 50 итераций — полосы по 500 строк
    for y0, y1, a0, a1 in _strips(fg.shape[0], halo=iterations, rows=max(strip_rows(fg.shape[1]), 10 * iterations)):
        band = fg[a0:a1]
        for _ in range(iterations):
            band = one_step(band)
        out[y0:y1] = band[y0 - a0:y0 - a0 + (y1 - y0)]
    return out


def dilate(fg: np.ndarray, iterations: int, count: int) -> np.ndarray:
    """Точка фона становится объектом, если объектов вокруг не меньше count. За краем — фон."""
    return _iterate_bands(fg, iterations, lambda b: b | (_neighbours(b, 0) >= count).view(np.uint8))


def erode(fg: np.ndarray, iterations: int, count: int, pad_edges: bool = True) -> np.ndarray:
    """Точка объекта становится фоном, если фона вокруг не меньше count. «pad» — за краем объект."""
    outside = 1 if pad_edges else 0
    return _iterate_bands(fg, iterations, lambda b: b & ((8 - _neighbours(b, outside)) < count).view(np.uint8))


# ---------- фильтры ----------

def variance3(img: np.ndarray) -> np.ndarray:
    """RankFilters «Variance» с радиусом 1 (ядро 3×3), край — ближайшая точка."""
    f = img.astype(np.float64)
    mean = cv2.blur(f, (3, 3), borderType=cv2.BORDER_REPLICATE)
    sq = cv2.blur(f * f, (3, 3), borderType=cv2.BORDER_REPLICATE)
    return np.maximum(sq - mean * mean, 0.0).astype(np.float32)


def smooth8(img: np.ndarray) -> np.ndarray:
    """ByteProcessor.smooth (BLUR_MORE): (сумма 3×3 + 4) / 9 нацело, край — ближайшая точка."""
    s = cv2.boxFilter(img.astype(np.int32), cv2.CV_32S, (3, 3), normalize=False, borderType=cv2.BORDER_REPLICATE)
    return ((s + 4) // 9).astype(np.uint8)


def hsb_saturation(rgb: np.ndarray) -> np.ndarray:
    """ColorProcessor.getHSB: (int)(s·255), s = (max − min)/max в float, как java.awt.Color."""
    mx = rgb.max(axis=2).astype(np.float32)
    mn = rgb.min(axis=2).astype(np.float32)
    s = np.where(mx > 0, (mx - mn) / np.where(mx > 0, mx, 1), 0).astype(np.float32)
    return (s * np.float32(255.0)).astype(np.int32).astype(np.uint8)


def multiply8(img: np.ndarray, k: float) -> np.ndarray:
    """ByteProcessor.multiply: округление и обрезка в 255."""
    return np.minimum(np.floor(img.astype(np.float64) * k + 0.5), 255).astype(np.uint8)


def display_range_after_variance(var: np.ndarray, saturated: float = 0.5) -> tuple[float, float]:
    """Диапазон показа, который ставит RankFilters после «Variance...» (ContrastEnhancer).

    Он же потом служит диапазоном гистограммы для порога стромы: «Add... value=1»
    значения меняет, а диапазон нет. Гистограмма — 256 ячеек от минимума до максимума
    всей картинки, отсекается по 0,25 % точек с каждой стороны.
    """
    vmin, vmax = float(var.min()), float(var.max())
    if vmax <= vmin:
        return vmin, vmax
    bins = 256
    scale = bins / (vmax - vmin)
    hist = np.zeros(bins, np.int64)
    for y0, y1, _, _ in _strips(var.shape[0], w=var.shape[1]):     # полосами: float64 и int64 на всю область — 16 байт на точку
        idx = np.minimum(((var[y0:y1].astype(np.float64) - vmin) * scale).astype(np.int64), bins - 1)
        hist += np.bincount(idx.ravel(), minlength=bins)
    threshold = int(var.size * saturated / 200.0)
    cum = np.cumsum(hist)
    hmin = int(np.argmax(cum > threshold))
    rcum = np.cumsum(hist[::-1])
    hmax = bins - 1 - int(np.argmax(rcum > threshold))
    if hmax <= hmin:
        return vmin, vmax
    size = (vmax - vmin) / bins
    return vmin + hmin * size, vmin + hmax * size


# ---------- разделение слипшихся (EDM «Watershed») ----------

def watershed(fg: np.ndarray) -> np.ndarray:
    """Process › Binary › Watershed: построчный перенос ImageJ в ij_watershed."""
    return ij_watershed.watershed(fg)


# ---------- частицы (ParticleAnalyzer, Wand, EllipseFitter) ----------

def traced_perimeter(obj: np.ndarray) -> float:
    """Периметр контура частицы, как PolygonRoi.getTracedPerimeter у контура Wand (8-связный)."""
    h, w = obj.shape

    def inside(x, y):
        return 0 <= x < w and 0 <= y < h and obj[y, x] != 0

    def inside_dir(x, y, d):
        d &= 3
        if d == 0:
            return inside(x, y)
        if d == 1:
            return inside(x, y - 1)
        if d == 2:
            return inside(x - 1, y - 1)
        return inside(x - 1, y)

    ys, xs = np.nonzero(obj)
    y0 = int(ys.min())
    x0 = int(xs[ys == y0].min())
    x = x0
    while inside(x, y0):
        x += 1
    start_x, start_y = x, y0
    # Wand.traceEdge: (x−1, y) внутри, (x, y) снаружи → идём вниз (направление 3)
    start_dir = 3
    y = start_y + 1
    start_y = y
    direction = start_dir
    px, py = [], []
    while True:
        nd = direction + 1
        while True:
            if inside_dir(x, y, nd):
                break
            nd -= 1
            if nd < direction:
                break
        if nd != direction:
            px.append(x)
            py.append(y)
        m = nd & 3
        if m == 0:
            x += 1
        elif m == 1:
            y -= 1
        elif m == 2:
            x -= 1
        else:
            y += 1
        direction = nd
        if x == start_x and y == start_y and (direction & 3) == start_dir:
            break
    if px and px[0] != x:
        px.append(x)
        py.append(y)
    n = len(px)
    if n < 4:
        return 0.0
    sumdx = sumdy = corners = 0
    dx1, dy1 = px[0] - px[n - 1], py[0] - py[n - 1]
    side1 = abs(dx1) + abs(dy1)
    corner = False
    for i in range(n):
        j = (i + 1) % n
        dx2, dy2 = px[j] - px[i], py[j] - py[i]
        sumdx += abs(dx1)
        sumdy += abs(dy1)
        side2 = abs(dx2) + abs(dy2)
        if side1 > 1 or not corner:
            corner = True
            corners += 1
        else:
            corner = False
        dx1, dy1, side1 = dx2, dy2, side2
    return sumdx + sumdy - corners * (2.0 - math.sqrt(2.0))


def ellipse_major(filled: np.ndarray) -> float:
    """Большая ось эллипса той же площади (EllipseFitter), в точках."""
    ys, xs = np.nonzero(filled)
    n = len(xs)
    xs = xs.astype(np.float64)
    ys = ys.astype(np.float64)
    x2 = (xs * xs).sum() + 0.08333333 * n
    y2 = (ys * ys).sum() + 0.08333333 * n
    xy = (xs * ys).sum()
    x1, y1 = xs.mean(), ys.mean()
    u20 = x2 / n - x1 * x1
    u02 = y2 / n - y1 * y1
    u11 = xy / n - x1 * y1
    m4 = 4.0 * abs(u02 * u20 - u11 * u11)
    if m4 < 0.000001:
        m4 = 0.000001
    a11, a12, a22 = u02 / m4, u11 / m4, u20 / m4
    tmp = a11 - a22
    if tmp == 0.0:
        tmp = 0.000001
    half_pi = math.pi / 2
    theta = 0.5 * math.atan(2.0 * a12 / tmp)
    if theta < 0.0:
        theta += half_pi
    if a12 > 0.0:
        theta += half_pi
    elif a12 == 0.0:
        if a22 > a11:
            theta = 0.0
            a11, a22 = a22, a11
        elif a11 != a22:
            theta = half_pi
    tmp = math.sin(theta) or 0.000001
    z = a12 * math.cos(theta) / tmp
    major = math.sqrt(1.0 / abs(a22 + z))
    minor = math.sqrt(1.0 / abs(a11 - z))
    scale = math.sqrt(n / (math.pi * major * minor))
    major, minor = major * scale * 2.0, minor * scale * 2.0
    return max(major, minor)


def fill_holes(obj: np.ndarray) -> np.ndarray:
    """Внутренность контура частицы (маска многоугольника Wand): частица с дырами."""
    h, w = obj.shape
    padded = np.zeros((h + 2, w + 2), np.uint8)
    padded[1:-1, 1:-1] = obj
    flood = padded.copy()
    cv2.floodFill(flood, np.zeros((h + 4, w + 4), np.uint8), (0, 0), 1, flags=4)
    # Дыра — всё, куда не дошла заливка снаружи по 4-связности (фон 8-связной частицы)
    return ((padded == 1) | (flood == 0))[1:-1, 1:-1].astype(np.uint8)


def filter_particles(cand: np.ndarray, pixel_um: float, p: Params) -> tuple[np.ndarray, dict]:
    """Analyze Particles (размер, circularity) и отбор по округлости, как filterAdipocytes."""
    unit2 = pixel_um * pixel_um
    min_px = p.adip_min_um2 / unit2
    max_px = p.adip_max_um2 / unit2
    out = np.zeros(cand.shape, np.uint8)
    kept_size_circ = kept = 0
    for x, y, w, h, obj in ij_watershed.components(cand):
        area = int(obj.sum())
        if area < min_px or area > max_px:
            continue
        if p.min_circularity > 0:
            per = traced_perimeter(obj)
            circ = 0.0 if per == 0 else 4.0 * math.pi * (area / (per * per))
            circ = min(circ, 1.0)
            if circ < p.min_circularity:
                continue
        kept_size_circ += 1
        filled = fill_holes(obj)
        n_filled = int(filled.sum())
        major = ellipse_major(filled)
        if (4 * n_filled) / (math.pi * major * major) <= p.min_roundness:
            continue
        kept += 1
        out[y:y + h, x:x + w] |= filled
    return out, {"particles_after_size_circ": kept_size_circ, "particles_after_round": kept}


# ---------- весь расчёт по области ----------

# Сколько байт на точку области держится в памяти на пике расчёта (замер 2026-09-23 на
# синтетическом фрагменте 17 млн точек, см. раздел 13.8 ТЗ) — для оценки памяти до запуска.
PEAK_BYTES_PER_PIXEL = 12
# Постоянная часть сверх библиотек: буферы чтения со скана (OpenSlide, PIL, копии — до 4 × 16 МБ
# при READ_BUDGET_PX), кэш OpenSlide (32 МБ), полосы float64 (до 64 МБ при STRIP_PIXELS)
TRANSIENT_MB = 130


STAGES = ("deconvolve", "variance", "bone", "imv", "hemato", "adip-watershed", "adip-shrink", "adip-particles")
# Доля времени каждой стадии (замер 2026-09-23, синтетический фрагмент 17 млн точек) — для показа хода
STAGE_WEIGHTS = {"deconvolve": 0.25, "variance": 0.04, "bone": 0.30, "imv": 0.04, "hemato": 0.03,
                 "adip-watershed": 0.05, "adip-shrink": 0.22, "adip-particles": 0.07}


def _clear_where(mask: np.ndarray, *conditions: np.ndarray) -> None:
    """mask &= ~condition для каждого условия, без временных массивов размером с область."""
    for cond in conditions:
        np.copyto(mask, False, where=cond)


def analyse(rgb, tissue: np.ndarray, art: np.ndarray | None, pixel_um: float,
            p: Params | None = None, keep_debug: bool = False, progress=None) -> Result:
    """rgb — область в разрешении расчёта: массив h×w×3 либо источник строк, у которого
    rgb[y0:y1] возвращает такой массив для полосы строк (SlideRows в cellularity_slide
    читает полосы со скана по мере надобности, и вся область в памяти не лежит).
    tissue, art — маски формы h×w. progress(стадия) вызывается в начале каждой из STAGES.

    Целиком в памяти держатся только однобайтовые слои и дисперсия (float32); разложение
    окрасок, отношение для кости и прочее с плавающей точкой считается полосами строк.
    Фильтры 3×3 получают строку соседней полосы, поэтому результат тот же, что целиком.
    Маски сочетаются на месте (_clear_where): «a & ~b» создавало бы две копии области.
    """
    p = p or Params()
    step = progress or (lambda stage: None)
    tissue = np.asarray(tissue, bool)
    art = np.zeros_like(tissue) if art is None else np.asarray(art, bool)
    h, w = tissue.shape
    dbg: dict = {}

    # проход 1: окраски → bimv (d0 − d1), hem (сглаженное d2 − d0), кандидаты в жир.
    # Полоса берётся с полем в одну строку ради сглаживания 3×3, поэтому само hd не хранится
    step("deconvolve")
    bimv = np.empty((h, w), np.uint8)
    hem = np.empty((h, w), np.uint8)
    cand = np.empty((h, w), bool)
    for y0, y1, a0, a1 in _strips(h, halo=1, w=w):
        block = np.asarray(rgb[a0:a1])
        c0, c1 = y0 - a0, y0 - a0 + (y1 - y0)
        d0, d1, d2 = deconvolve(block)
        hd = sub8(d2, d0)
        hem[y0:y1] = smooth8(hd)[c0:c1]
        bimv[y0:y1] = sub8(d0, d1)[c0:c1]
        adipv = cv2.add(hd[c0:c1], multiply8(hsb_saturation(block[c0:c1]), 8))
        cand[y0:y1] = adipv <= 200
        del block, d0, d1, d2, hd, adipv
    step("variance")
    var_raw = np.empty((h, w), np.float32)
    for y0, y1, a0, a1 in _strips(h, halo=1, w=w):
        var_raw[y0:y1] = variance3(bimv[a0:a1])[y0 - a0:y0 - a0 + (y1 - y0)]

    one = np.float32(1.0)

    def ratio_strip(y0, y1):
        return (bimv[y0:y1].astype(np.float32) / (var_raw[y0:y1] + one)).astype(np.float32)

    step("bone")
    # boneIMVFinder: порог по отношению bimv / (дисперсия + 1)
    rmin, rmax = np.inf, -np.inf
    for y0, y1, _, _ in _strips(h, w=w):
        r = ratio_strip(y0, y1)
        rmin, rmax = min(rmin, float(r.min())), max(rmax, float(r.max()))
    hist = np.zeros(256, np.int64)
    for y0, y1, _, _ in _strips(h, w=w):
        hist += np.bincount(to_byte(ratio_strip(y0, y1)[tissue[y0:y1]], rmin, rmax), minlength=256)
    lower = _dark_lower(hist, rmin, rmax, "MaxEntropy")
    dbg.update(bone_min=rmin, bone_max=rmax, bone_lower=lower)
    bone = np.empty((h, w), np.uint8)
    for y0, y1, _, _ in _strips(h, w=w):
        bone[y0:y1] = ratio_strip(y0, y1) >= np.float32(lower)
    bone = dilate(bone, 5, 1)
    bone = erode(dilate(bone, 20, 2), 20, 2, pad_edges=True).view(bool)
    bone &= tissue
    _clear_where(bone, art)

    step("imv")
    # строма и сосуды: порог по дисперсии + 1; кость и всё вне ткани — Float.MAX_VALUE
    vmin, vmax = display_range_after_variance(var_raw)
    dbg.update(var_min=vmin, var_max=vmax)
    hist = np.zeros(256, np.int64)
    for y0, y1, _, _ in _strips(h, w=w):
        t = tissue[y0:y1]
        b = bone[y0:y1]
        v = var_raw[y0:y1] + one
        hist += np.bincount(to_byte(v[t & ~b], vmin, vmax), minlength=256)
        hist[255] += int((t & b).sum())          # MAX_VALUE → последняя ячейка
    lower = _dark_lower(hist, vmin, vmax, "MaxEntropy")
    dbg["var_lower"] = lower
    imv = np.empty((h, w), bool)
    for y0, y1, _, _ in _strips(h, w=w):
        imv[y0:y1] = (var_raw[y0:y1] + one) >= np.float32(lower)
    imv &= tissue
    _clear_where(imv, bone, art)
    del var_raw, bimv

    step("hemato")
    # hematoFinder
    hist = np.zeros(256, np.int64)
    for y0, y1, _, _ in _strips(h, w=w):
        hist += np.bincount(hem[y0:y1][tissue[y0:y1]], minlength=256)
    t = auto_threshold(hist, "Default")
    dbg["hem_lower"] = float(t + 1)
    hemato = dilate((hem >= t + 1).view(np.uint8), 5, 2).view(bool)
    hemato &= tissue
    _clear_where(hemato, bone, art)
    del hem
    _clear_where(imv, hemato)

    step("adip-watershed")
    # adipFinder
    cand &= tissue
    _clear_where(cand, bone, art)
    if keep_debug:
        dbg["adip_raw"] = cand.copy()
    cand = watershed(cand)
    if keep_debug:
        dbg["adip_ws"] = cand.copy()
    step("adip-shrink")
    # «Dilate» при сброшенном «чёрном фоне»: объект — точки 0, то есть сужение кандидатов
    # со счётом 5; за краем картинки для dilate — фон, то есть кандидат (255)
    cand ^= 1
    cand = dilate(cand, 50, 5)
    cand ^= 1
    if keep_debug:
        dbg["candidates"] = cand.copy()
    step("adip-particles")
    adip, stats = filter_particles(cand, pixel_um, p)
    del cand
    dbg.update(stats)
    adip = adip.view(bool)
    _clear_where(imv, adip)
    _clear_where(hemato, adip)
    n_adip = ij_watershed.count_components(adip.view(np.uint8))
    return Result(bone=bone, imv=imv, hemato=hemato, adip=adip, n_adip=n_adip, debug=dbg)


def _dark_lower(hist: np.ndarray, vmin: float, vmax: float, method: str) -> float:
    """Нижний порог «<метод> dark no-reset» у 32-битного изображения по готовой гистограмме."""
    t = auto_threshold(hist, method)
    lower = min(t + 1, 255)
    return float(np.float32(vmin + (lower / 255.0) * (vmax - vmin)))
