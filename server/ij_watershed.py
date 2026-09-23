"""Разделение слипшихся частиц как Process › Binary › Watershed в ImageJ 1.54f.

Построчный перенос MaximumFinder.findMaxima(…, SEGMENTED, isEDM=true) с тем же
порядком обхода: результат зависит от порядка, в котором точки заливаются по
уровням, поэтому векторизовать его без расхождений нельзя. Циклы скомпилированы
Numba. Соответствие методам ImageJ указано в именах функций.

Вход — карта расстояний (float32, 0 — фон), выход — маска 0/255 после разделения.
"""
from __future__ import annotations

import math

import cv2
import numpy as np
from numba import njit

MAXIMUM, LISTED, PROCESSED, MAX_AREA, EQUAL, MAX_POINT, ELIMINATED = 1, 2, 4, 8, 16, 32, 64
SQRT2 = np.float32(1.4142135624)
DX = np.array([0, 1, 1, 1, 0, -1, -1, -1], np.int64)
DY = np.array([-1, -1, 0, 1, 1, 1, 0, -1], np.int64)


def components(fg: np.ndarray):
    """Связные (8-связность) части двоичной маски без карты меток.

    Карта меток (int32) стоила бы 4 байта на точку области; вместо неё — внешние
    контуры (findContours) и заливка части от точки её контура в своём прямоугольнике.
    RETR_CCOMP, а не RETR_EXTERNAL: часть, лежащая в дыре другой части, тоже верхнего
    уровня. Даёт (x, y, w, h, часть 0/1 в своём прямоугольнике).
    """
    fg = np.asarray(fg, np.uint8)
    contours, hierarchy = cv2.findContours(fg, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_SIMPLE)
    if hierarchy is None:
        return
    for contour, (_, _, _, parent) in zip(contours, hierarchy[0]):
        if parent != -1:
            continue
        x, y, w, h = cv2.boundingRect(contour)
        window = fg[y:y + h, x:x + w].copy()
        sx, sy = (int(v) for v in contour[0][0])
        cv2.floodFill(window, None, (sx - x, sy - y), 2, flags=8)
        yield x, y, w, h, (window == 2).view(np.uint8)


def count_components(fg: np.ndarray) -> int:
    """Число связных (8) частей маски — без карты меток."""
    _, hierarchy = cv2.findContours(np.asarray(fg, np.uint8), cv2.RETR_CCOMP, cv2.CHAIN_APPROX_SIMPLE)
    return 0 if hierarchy is None else int((hierarchy[0][:, 3] == -1).sum())


def edm(mask: np.ndarray) -> np.ndarray:
    """EDM.makeFloatEDM(ip, 0, edgesAreBackground=false) — тот же алгоритм, что в ImageJ.

    Не точное евклидово расстояние: ImageJ протягивает ближайшую точку фона от соседей
    за два прохода, и в редких точках ошибается на сотые. OpenCV считает точно, но его
    значения зависят от размера картинки в последнем знаке, а разделению этого хватает,
    чтобы разойтись с ImageJ. Край картинки фоном не считается.
    """
    m = np.ascontiguousarray((mask > 0).astype(np.uint8))
    h, w = m.shape
    return _make_float_edm(m.ravel(), w, h).reshape(h, w)


@njit(cache=True)
def _min_dist2(points, p_prev, p_diag, x, y, dist_sqr):
    p0 = points[x]
    nearest = p0
    if p0 != -1:
        x0 = p0 & 0xFFFF
        y0 = (p0 >> 16) & 0xFFFF
        d = (x - x0) * (x - x0) + (y - y0) * (y - y0)
        if d < dist_sqr:
            dist_sqr = d
    if p_diag != p0 and p_diag != -1:
        x1 = p_diag & 0xFFFF
        y1 = (p_diag >> 16) & 0xFFFF
        d = (x - x1) * (x - x1) + (y - y1) * (y - y1)
        if d < dist_sqr:
            nearest = p_diag
            dist_sqr = d
    if p_prev != p_diag and p_prev != -1:
        x1 = p_prev & 0xFFFF
        y1 = (p_prev >> 16) & 0xFFFF
        d = (x - x1) * (x - x1) + (y - y1) * (y - y1)
        if d < dist_sqr:
            nearest = p_prev
            dist_sqr = d
    points[x] = nearest
    return np.float32(dist_sqr)


@njit(cache=True)
def _edm_line(b, f, buf0, buf1, w, offset, y):
    big = 2147483647                      # Integer.MAX_VALUE: край не фон
    points = buf0
    p_prev = -1
    p_diag = -1
    for x in range(w):
        p_next_diag = points[x]
        if b[offset] == 0:
            points[x] = x | (y << 16)
        else:
            d2 = _min_dist2(points, p_prev, p_diag, x, y, big)
            if f[offset] > d2:
                f[offset] = d2
        p_prev = points[x]
        p_diag = p_next_diag
        offset += 1
    offset -= 1
    points = buf1
    p_prev = -1
    p_diag = -1
    for x in range(w - 1, -1, -1):
        p_next_diag = points[x]
        if b[offset] == 0:
            points[x] = x | (y << 16)
        else:
            d2 = _min_dist2(points, p_prev, p_diag, x, y, big)
            if f[offset] > d2:
                f[offset] = d2
        p_prev = points[x]
        p_diag = p_next_diag
        offset -= 1


@njit(cache=True)
def _make_float_edm(b, w, h):
    n = w * h
    f = np.zeros(n, np.float32)
    for i in range(n):
        if b[i] != 0:
            f[i] = np.float32(3.4028235e38)
    buf0 = np.full(w, -1, np.int64)
    buf1 = np.full(w, -1, np.int64)
    for y in range(h):
        _edm_line(b, f, buf0, buf1, w, y * w, y)
    buf0[:] = -1
    buf1[:] = -1
    for y in range(h - 1, -1, -1):
        _edm_line(b, f, buf0, buf1, w, y * w, y)
    for i in range(n):
        f[i] = np.float32(math.sqrt(f[i]))
    return f


@njit(cache=True)
def _within(x, y, d, w, h):
    if d == 0:
        return y > 0
    if d == 1:
        return x < w - 1 and y > 0
    if d == 2:
        return x < w - 1
    if d == 3:
        return x < w - 1 and y < h - 1
    if d == 4:
        return y < h - 1
    if d == 5:
        return x > 0 and y < h - 1
    if d == 6:
        return x > 0
    return x > 0 and y > 0


@njit(cache=True)
def _true_edm_height(px, x, y, w, h, off):
    i = x + y * w
    v = px[i]
    if x == 0 or y == 0 or x == w - 1 or y == h - 1 or v == 0:
        return v
    true_h = np.float32(v + np.float32(0.5) * np.float32(1.4142135624))
    ridge = False
    for d in range(4):
        d2 = (d + 4) % 8
        v1 = px[i + off[d]]
        v2 = px[i + off[d2]]
        if v >= v1 and v >= v2:
            ridge = True
            hh = np.float32((v1 + v2) / np.float32(2))
        else:
            hh = min(v1, v2)
        if d % 2 == 0:
            hh = np.float32(hh + np.float32(1))
        else:
            hh = np.float32(hh + np.float32(1.4142135624))
        if true_h > hh:
            true_h = hh
    if not ridge:
        true_h = v
    return true_h


@njit(cache=True)
def _find_maxima_segmented(px, w, h, gmin, gmax):
    """Вся цепочка для одной картинки (уровни непустые — свои же)."""
    pixels = _prepare_levels(px, w, h, gmin, gmax)
    nonempty = np.zeros(256, np.bool_)
    for i in range(w * h):
        nonempty[pixels[i]] = True
    return _segment(pixels, w, h, nonempty)


@njit(cache=True)
def _prepare_levels(px, w, h, gmin, gmax):
    """Вершины (getSortedMaxPoints, analyzeAndMarkMaxima), make8bit и cleanupMaxima."""
    n = w * h
    off = np.array([-w, -w + 1, 1, w + 1, w, w - 1, -1, -w - 1], np.int64)
    dx = np.array([0, 1, 1, 1, 0, -1, -1, -1], np.int64)
    dy = np.array([-1, -1, 0, 1, 1, 1, 0, -1], np.int64)
    types = np.zeros(n, np.uint8)
    if not gmax > gmin:
        return np.zeros(n, np.uint8)

    # ---------- getSortedMaxPoints ----------
    n_max = 0
    for y in range(h):
        for x in range(w):
            i = x + y * w
            v = px[i]
            v_true = _true_edm_height(px, x, y, w, h, off)
            if v == gmin:
                continue
            is_max = True
            inner = y != 0 and y != h - 1 and x != 0 and x != w - 1
            for d in range(8):
                if inner or _within(x, y, d, w, h):
                    xn = x + dx[d]
                    yn = y + dy[d]
                    vn = px[xn + yn * w]
                    vn_true = _true_edm_height(px, xn, yn, w, h, off)
                    if vn > v and vn_true > v_true:
                        is_max = False
                        break
            if is_max:
                types[i] = MAXIMUM
                n_max += 1
    vfactor = np.float32(2e9 / np.float64(gmax - gmin))
    keys = np.empty(n_max, np.int64)
    k = 0
    for y in range(h):
        for x in range(w):
            p = x + y * w
            if types[p] == MAXIMUM:
                fv = _true_edm_height(px, x, y, w, h, off)
                iv = np.int64(np.int32(np.float32((fv - gmin) * vfactor)))
                keys[k] = (iv << 32) | p
                k += 1
    keys.sort()

    # ---------- analyzeAndMarkMaxima (strict=false, SEGMENTED) ----------
    max_sorting_error = np.float32(np.float32(1.1) * (np.float32(1.4142135624) / np.float32(2)))
    tolerance = np.float32(0.5)
    plist = np.empty(n, np.int32)
    for im in range(n_max - 1, -1, -1):
        offset0 = keys[im] & 0xFFFFFFFF
        if (types[offset0] & PROCESSED) != 0:
            continue
        x0 = offset0 % w
        y0 = offset0 // w
        v0 = _true_edm_height(px, x0, y0, w, h, off)
        while True:
            plist[0] = offset0
            types[offset0] |= (EQUAL | LISTED)
            list_len = 1
            list_i = 0
            sorting_error = False
            max_possible = True
            while True:
                offset = plist[list_i]
                x = offset % w
                y = offset // w
                inner = y != 0 and y != h - 1 and x != 0 and x != w - 1
                for d in range(8):
                    offset2 = offset + off[d]
                    if (inner or _within(x, y, d, w, h)) and (types[offset2] & LISTED) == 0:
                        if px[offset2] <= 0:
                            continue
                        if (types[offset2] & PROCESSED) != 0:
                            max_possible = False
                            break
                        x2 = x + dx[d]
                        y2 = y + dy[d]
                        v2 = _true_edm_height(px, x2, y2, w, h, off)
                        if v2 > np.float32(v0 + max_sorting_error):
                            max_possible = False
                            break
                        elif v2 >= np.float32(v0 - tolerance):
                            if v2 > v0:
                                sorting_error = True
                                offset0 = offset2
                                v0 = v2
                                x0 = x2
                                y0 = y2
                            plist[list_len] = offset2
                            list_len += 1
                            types[offset2] |= LISTED
                            if v2 == v0:
                                types[offset2] |= EQUAL
                list_i += 1
                if not list_i < list_len:
                    break
            if sorting_error:
                for li in range(list_len):
                    types[plist[li]] = 0
                continue
            reset = LISTED if max_possible else (LISTED | EQUAL)
            for li in range(list_len):
                o = plist[li]
                types[o] &= np.uint8(255 - reset)
                types[o] |= PROCESSED
                if max_possible:
                    types[o] |= MAX_AREA
            break
    if n_max == 0:
        for i in range(n):
            types[i] = PROCESSED | MAX_AREA

    # ---------- make8bit (isEDM) ----------
    pixels = np.zeros(n, np.uint8)
    min_value = 1.0
    offset_v = min_value - (np.float64(gmax) - min_value) * (1.0 / 253 / 2 - 1e-6)
    factor = 253 / (np.float64(gmax) - min_value)
    if factor > 1:
        factor = 1.0
    for i in range(n):
        raw = px[i]
        if raw < 0.5:
            pixels[i] = 0
        elif (types[i] & MAX_AREA) != 0:
            pixels[i] = 255
        else:
            v = 1 + np.int64(math.floor((np.float64(raw) - offset_v) * factor + 0.5))
            if v < 1:
                pixels[i] = 1
            elif v <= 254:
                pixels[i] = np.uint8(v)
            else:
                pixels[i] = 254

    # ---------- cleanupMaxima ----------
    for im in range(n_max - 1, -1, -1):
        offset0 = keys[im] & 0xFFFFFFFF
        if (types[offset0] & (MAX_AREA | ELIMINATED)) != 0:
            continue
        level = np.int64(pixels[offset0])
        lo_level = level + 1
        plist[0] = offset0
        types[offset0] |= LISTED
        list_len = 1
        last_len = 1
        saddle = False
        while not saddle and lo_level > 0:
            lo_level -= 1
            last_len = list_len
            list_i = 0
            while True:
                offset = plist[list_i]
                x = offset % w
                y = offset // w
                inner = y != 0 and y != h - 1 and x != 0 and x != w - 1
                for d in range(8):
                    offset2 = offset + off[d]
                    if (inner or _within(x, y, d, w, h)) and (types[offset2] & LISTED) == 0:
                        if (types[offset2] & MAX_AREA) != 0 or ((types[offset2] & ELIMINATED) != 0 and pixels[offset2] >= lo_level):
                            saddle = True
                            break
                        elif pixels[offset2] >= lo_level and (types[offset2] & ELIMINATED) == 0:
                            plist[list_len] = offset2
                            list_len += 1
                            types[offset2] |= LISTED
                if saddle:
                    break
                list_i += 1
                if not list_i < list_len:
                    break
        for li in range(list_len):
            types[plist[li]] &= np.uint8(255 - LISTED)
        for li in range(last_len):
            o = plist[li]
            pixels[o] = np.uint8(lo_level)
            types[o] |= ELIMINATED

    return pixels


@njit(cache=True)
def _segment(pixels, w, h, nonempty):
    """watershedSegment и watershedPostProcess.

    nonempty — непустые уровни всей картинки: недостигнутые точки уровня ImageJ
    переносит на ближайший ниже непустой уровень всей картинки, а не только своей части.
    """
    n = w * h
    off = np.array([-w, -w + 1, 1, w + 1, w, w - 1, -1, -w - 1], np.int64)
    out = np.zeros(n, np.uint8)
    hist = np.zeros(256, np.int64)
    for i in range(n):
        hist[pixels[i]] += 1
    array_size = n - hist[0] - hist[255]
    coords = np.zeros(max(array_size, 1), np.int32)
    level_start = np.zeros(256, np.int64)
    highest = 0
    o = 0
    for v in range(1, 255):
        level_start[v] = o
        o += hist[v]
        if hist[v] > 0:
            highest = v
    level_offset = np.zeros(highest + 1, np.int64)
    for i in range(n):
        v = pixels[i]
        if v > 0 and v < 255:
            coords[level_start[v] + level_offset[v]] = i
            level_offset[v] += 1
    table = _fate_table()
    seq = np.array([7, 3, 1, 5, 0, 4, 2, 6], np.int64)
    set_list = np.empty(max(array_size, 1), np.int32)
    for level in range(highest, 0, -1):
        remaining = hist[level]
        idle = 0
        while remaining > 0 and idle < 8:
            d_index = 0
            while True:
                nch = _process_level(seq[d_index % 8], pixels, table, level_start[level], remaining,
                                     coords, set_list, w, h)
                remaining -= nch
                if nch > 0:
                    idle = 0
                d_index += 1
                cont = remaining > 0 and idle < 8
                idle += 1
                if not cont:
                    break
        if remaining > 0 and level > 1:
            next_level = level
            while True:
                next_level -= 1
                if not (next_level > 1 and hist[next_level] == 0 and not nonempty[next_level]):
                    break
            if next_level > 0:
                new_end = level_start[next_level] + hist[next_level]
                p = level_start[level]
                for _ in range(remaining):
                    i = coords[p]
                    p += 1
                    x = i % w
                    y = i // w
                    add = False
                    if x == 0 or y == 0 or x == w - 1 or y == h - 1:
                        add = True
                    else:
                        for d in range(8):
                            if _within(x, y, d, w, h) and pixels[i + off[d]] == 0:
                                add = True
                                break
                    if add:
                        coords[new_end] = i
                        new_end += 1
                hist[next_level] = new_end - level_start[next_level]

    # ---------- watershedPostProcess ----------
    for i in range(n):
        out[i] = 255 if pixels[i] == 255 else 0
    return out


@njit(cache=True)
def _process_level(pas, pixels, table, start, npoints, coords, set_list, w, h):
    xmax = w - 1
    ymax = h - 1
    n_changed = 0
    n_unchanged = 0
    for k in range(npoints):
        i = coords[start + k]
        x = i % w
        y = i // w
        index = 0
        if y > 0 and pixels[i - w] == 255:
            index ^= 1
        if x < xmax and y > 0 and pixels[i - w + 1] == 255:
            index ^= 2
        if x < xmax and pixels[i + 1] == 255:
            index ^= 4
        if x < xmax and y < ymax and pixels[i + w + 1] == 255:
            index ^= 8
        if y < ymax and pixels[i + w] == 255:
            index ^= 16
        if x > 0 and y < ymax and pixels[i + w - 1] == 255:
            index ^= 32
        if x > 0 and pixels[i - 1] == 255:
            index ^= 64
        if x > 0 and y > 0 and pixels[i - w - 1] == 255:
            index ^= 128
        mask = 1 << pas
        if (table[index] & mask) == mask:
            set_list[n_changed] = i
            n_changed += 1
        else:
            coords[start + n_unchanged] = i
            n_unchanged += 1
    for k in range(n_changed):
        pixels[set_list[k]] = 255
    return n_changed


@njit(cache=True)
def _fate_table():
    table = np.zeros(256, np.int64)
    is_set = np.zeros(8, np.bool_)
    for item in range(256):
        mask = 1
        for i in range(8):
            is_set[i] = (item & mask) == mask
            mask *= 2
        mask = 1
        for i in range(8):
            if is_set[(i + 4) % 8]:
                table[item] |= mask
            mask *= 2
        for i in range(0, 8, 2):
            if is_set[i]:
                is_set[(i + 1) % 8] = True
                is_set[(i + 7) % 8] = True
        transitions = 0
        for i in range(8):
            if is_set[i] != is_set[(i + 1) % 8]:
                transitions += 1
        if transitions >= 4:
            table[item] = 0
    return table


def watershed(mask: np.ndarray) -> np.ndarray:
    """Маска 0/1 → маска 0/1 после разделения, как команда «Watershed» (чёрный фон).

    Ради памяти разделение идёт по связным частям маски, каждая — в своём прямоугольнике
    с рамкой фона в одну точку. Результат тот же, что по всей картинке:
    - заливка и поиск вершин не выходят за пределы части (соседи — только фон);
    - ближайшая к точке части точка фона всегда соседняя с частью и попадает в рамку;
    - минимум и максимум карты расстояний, от которых в ImageJ зависят сортировка вершин
      и перевод в 8 бит, берутся по всей картинке и передаются каждой части.
    Со стороны края картинки рамки нет: край, как и в ImageJ, фоном не считается.

    Память (КЛ-7): части выделяются по контурам, без карты меток (4 байта на точку);
    в каждом проходе в памяти одна часть с рамкой, её уровни во втором и третьем
    проходах считаются заново, а не хранятся для всех частей сразу.
    """
    fg = (mask > 0).view(np.uint8) if mask.dtype == np.bool_ else (mask > 0).astype(np.uint8)
    out = np.zeros_like(fg)
    if not fg.any():
        return out
    H, W = fg.shape

    def framed():
        """Каждая часть в прямоугольнике с рамкой фона в одну точку (кроме края картинки)."""
        for x, y, w, h, part in components(fg):
            x0, y0, x1, y1 = max(x - 1, 0), max(y - 1, 0), min(x + w + 1, W), min(y + h + 1, H)
            box = np.zeros((y1 - y0, x1 - x0), np.uint8)
            box[y - y0:y - y0 + h, x - x0:x - x0 + w] = part
            yield x0, y0, x1, y1, box

    def levels(box):
        h, w = box.shape
        return _prepare_levels(edm(box).ravel(), w, h, gmin, gmax)

    # первый проход — только максимум карты расстояний; сами карты не храним
    gmax = np.float32(0.0)
    for _, _, _, _, box in framed():
        gmax = max(gmax, np.float32(edm(box).max()))
    gmin = np.float32(0.0)
    if not (fg == 0).any():               # фона нет вовсе: как ImageJ, по всей карте
        gmin = np.float32(edm(fg).min())
    # второй проход: общий набор непустых уровней всех частей
    nonempty = np.zeros(256, np.bool_)
    for _, _, _, _, box in framed():
        nonempty[np.unique(levels(box))] = True
    # третий: заливка по уровням
    for x0, y0, x1, y1, box in framed():
        h, w = box.shape
        seg = _segment(levels(box), w, h, nonempty).reshape(h, w)
        out[y0:y1, x0:x1] |= (seg == 255).view(np.uint8) & box
    return out

