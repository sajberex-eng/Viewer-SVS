"""Клеточность по скану: фрагменты, чтение области и итог (этап 11, раздел 13.8 ТЗ).

Алгоритм — в cellularity.py; здесь то, что в MarrowQuant делает QuPath:
контуры ткани и артефактов, чтение области в разрешении расчёта, площади и формулы.

Разрешение расчёта (КЛ-5):
- «original» — как MarrowQuant: полное разрешение скана, уменьшенное в 4 раза
  (скан 20× считается при ~2 мкм на точку, скан 40× — при ~1 мкм);
- число — мкм на точку, одинаково для любых сканеров (1 — как объектив 10×, 2 — 5×).

Контуры (КЛ-2): из файла GeoJSON (экспорт QuPath: классы «Tissue Boundaries» и
«Artifact», координаты в точках полного разрешения) либо найденные по миниатюре.
"""
from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np

from . import cellularity

ORIGINAL = "original"
MQ_DOWNSAMPLE = 4.0
STRIP_ROWS = 512               # строк области за одно чтение из скана
TISSUE_CLASSES = {"tissue boundaries", "tissue"}
ARTIFACT_CLASSES = {"artifact"}
MIN_FRAGMENT_MM2 = 0.5         # мельче — не фрагмент, а крошка или пыль


@dataclass
class Fragment:
    """Фрагмент ткани: прямоугольник в точках полного разрешения и контуры внутри."""
    bbox: tuple[int, int, int, int]            # x0, y0, x1, y1 уровня 0
    tissue: list = field(default_factory=list)  # многоугольники [[x, y], …] (первый — внешний, дальше дыры)
    artifacts: list = field(default_factory=list)
    thumb_mask: np.ndarray | None = None        # для найденных автоматически: маска на миниатюре
    thumb_ds: float = 1.0


# ---------- контуры ----------

def _polygons(geometry: dict) -> list[list[list[list[float]]]]:
    """GeoJSON Polygon / MultiPolygon → список многоугольников (кольца координат)."""
    kind = geometry.get("type")
    if kind == "Polygon":
        return [geometry["coordinates"]]
    if kind == "MultiPolygon":
        return list(geometry["coordinates"])
    return []


def contours_from_geojson(path: Path) -> list[Fragment]:
    """Экспорт QuPath (File › Export objects › GeoJSON): ткань и артефакты по классам."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    # QuPath выгружает либо FeatureCollection, либо просто список объектов
    if isinstance(data, dict):
        features = data.get("features", [data])
    else:
        features = data
    tissues, artifacts = [], []
    for feature in features:
        props = feature.get("properties") or {}
        cls = props.get("classification") or {}
        # класс может отсутствовать вовсе: в QuPath контур можно нарисовать без класса
        name = (cls.get("name") if isinstance(cls, dict) else cls) or ""
        name = str(name).strip().lower()
        for poly in _polygons(feature.get("geometry") or {}):
            (tissues if name in TISSUE_CLASSES else artifacts if name in ARTIFACT_CLASSES else []).append(poly)
    fragments = []
    for poly in tissues:
        xs = [x for ring in poly for x, _ in ring]
        ys = [y for ring in poly for _, y in ring]
        bbox = (math.floor(min(xs)), math.floor(min(ys)), math.ceil(max(xs)), math.ceil(max(ys)))
        fragments.append(Fragment(bbox=bbox, tissue=poly))
    # артефакт относится к фрагменту, в прямоугольник которого попадает его центр
    for poly in artifacts:
        cx = sum(x for x, _ in poly[0]) / len(poly[0])
        cy = sum(y for _, y in poly[0]) / len(poly[0])
        for fragment in fragments:
            x0, y0, x1, y1 = fragment.bbox
            if x0 <= cx <= x1 and y0 <= cy <= y1:
                fragment.artifacts.append(poly)
                break
    return fragments


def contours_auto(slide, base_mpp: float) -> list[Fragment]:
    """Фрагменты по насыщенности цвета миниатюры (~16 мкм на точку); жир внутри ткани
    заполняется. Так контуры предлагаются пользователю, а не заменяют его обводку."""
    level = slide.get_best_level_for_downsample(16 / base_mpp) if hasattr(slide, "get_best_level_for_downsample") \
        else len(slide.level_downsamples) - 1
    ds = slide.level_downsamples[level]
    thumb = _rgb(slide.read_region((0, 0), level, slide.level_dimensions[level]))
    hsv = cv2.cvtColor(thumb, cv2.COLOR_RGB2HSV_FULL)
    mask = ((hsv[..., 1] > 25) & (hsv[..., 2] > 40)).astype(np.uint8)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8))
    flood = mask.copy()
    h, w = mask.shape
    cv2.floodFill(flood, np.zeros((h + 2, w + 2), np.uint8), (0, 0), 1)
    mask |= 1 - flood
    n, lab, stats, _ = cv2.connectedComponentsWithStats(mask)
    um2 = (ds * base_mpp) ** 2
    fragments = []
    for k in sorted(range(1, n), key=lambda k: -stats[k, 4]):
        if stats[k, 4] * um2 < MIN_FRAGMENT_MM2 * 1e6:
            continue
        x, y, bw, bh, _ = (int(v) for v in stats[k])
        fragments.append(Fragment(
            bbox=(int(x * ds), int(y * ds), int((x + bw) * ds), int((y + bh) * ds)),
            thumb_mask=(lab[y:y + bh, x:x + bw] == k).astype(np.uint8), thumb_ds=ds))
    return fragments


# ---------- область в разрешении расчёта ----------

def _rgb(region) -> np.ndarray:
    """PIL RGBA → RGB; прозрачное (за краем скана) — белое."""
    arr = np.asarray(region)
    if arr.ndim == 3 and arr.shape[2] == 4:
        rgb = arr[..., :3].copy()
        rgb[arr[..., 3] == 0] = 255
        return rgb
    return np.ascontiguousarray(arr[..., :3])


def analysis_size(bbox, factor: float) -> tuple[int, int]:
    x0, y0, x1, y1 = bbox
    return max(1, int(round((x1 - x0) / factor))), max(1, int(round((y1 - y0) / factor)))


def read_area(slide, bbox, factor: float) -> np.ndarray:
    """Прямоугольник скана в разрешении расчёта (factor точек уровня 0 на точку).

    Уровень пирамиды — самый грубый, который не грубее нужного, с запасом 2 %:
    у Aperio шаг уровней 4,0001, и строгое сравнение уводило бы на полное разрешение.
    Читается полосами, чтобы не держать в памяти большую область полного разрешения.
    """
    x0, y0, x1, y1 = bbox
    w, h = analysis_size(bbox, factor)
    level = max(i for i, d in enumerate(slide.level_downsamples) if d <= factor * 1.02)
    lds = slide.level_downsamples[level]
    sx = (x1 - x0) / w                        # точек уровня 0 на точку расчёта по x и y
    sy = (y1 - y0) / h
    out = np.empty((h, w, 3), np.uint8)
    for r0 in range(0, h, STRIP_ROWS):
        r1 = min(r0 + STRIP_ROWS, h)
        ly0 = y0 + r0 * sy
        lw = int(round((x1 - x0) / lds))
        lh = max(1, int(round((r1 - r0) * sy / lds)))
        strip = _rgb(slide.read_region((x0, int(round(ly0))), level, (lw, lh)))
        if strip.shape[1] != w or strip.shape[0] != r1 - r0:
            strip = cv2.resize(strip, (w, r1 - r0), interpolation=cv2.INTER_AREA)
        out[r0:r1] = strip
    return out


def rasterize(polygons: list, bbox, factor: float, shape) -> np.ndarray:
    """Многоугольники уровня 0 → маска области, как PolygonFiller в ImageJ.

    Точка (x, y) закрашена, если её центр (x + ½, y + ½) внутри контура: по каждой
    строке ищутся пересечения с рёбрами, закрашиваются точки с x + ½ в [xa, xb).
    Кольца дыр учитываются правилом чёт-нечет, как у контуров QuPath с дырами.
    """
    h, w = shape
    mask = np.zeros(shape, bool)
    x0, y0 = bbox[0], bbox[1]
    for poly in polygons:
        edges = []
        for ring in poly:
            pts = (np.asarray(ring, np.float64) - (x0, y0)) / factor
            a, b = pts, np.roll(pts, -1, axis=0)
            edges.append(np.column_stack([a, b]))
        e = np.concatenate(edges)
        e = e[e[:, 1] != e[:, 3]]                  # горизонтальные рёбра не пересекаются со строкой
        if not len(e):
            continue
        ymin = max(int(np.floor(min(e[:, 1].min(), e[:, 3].min()))), 0)
        ymax = min(int(np.ceil(max(e[:, 1].max(), e[:, 3].max()))), h)
        ax, ay, bx, by = e[:, 0], e[:, 1], e[:, 2], e[:, 3]
        layer = np.zeros(shape, bool)
        for y in range(ymin, ymax):
            yc = y + 0.5
            hit = ((ay <= yc) & (yc < by)) | ((by <= yc) & (yc < ay))
            if not hit.any():
                continue
            xs = np.sort(ax[hit] + (yc - ay[hit]) * (bx[hit] - ax[hit]) / (by[hit] - ay[hit]))
            for xa, xb in zip(xs[0::2], xs[1::2]):
                i0 = max(int(np.ceil(xa - 0.5)), 0)
                i1 = min(int(np.ceil(xb - 0.5)), w)
                if i1 > i0:
                    layer[y, i0:i1] ^= True
        mask |= layer
    return mask


def fragment_masks(fragment: Fragment, factor: float, shape) -> tuple[np.ndarray, np.ndarray]:
    if fragment.thumb_mask is not None:
        h, w = shape
        tissue = cv2.resize(fragment.thumb_mask, (w, h), interpolation=cv2.INTER_NEAREST).astype(bool)
        return tissue, np.zeros(shape, bool)
    tissue = rasterize([fragment.tissue], fragment.bbox, factor, shape)
    art = rasterize(fragment.artifacts, fragment.bbox, factor, shape) if fragment.artifacts else np.zeros(shape, bool)
    return tissue, art


# ---------- итог ----------

def areas(res: cellularity.Result, tissue: np.ndarray, art: np.ndarray) -> dict:
    """Площади в точках по частям — как sendResultsToQuPath (прочее — остаток)."""
    a = {
        "tissue": int(tissue.sum()),
        "artifacts": int((art & tissue).sum()),
        "bone": int(res.bone.sum()),
        "hemato": int(res.hemato.sum()),
        "adip": int(res.adip.sum()),
        "imv": int(res.imv.sum()),
    }
    a["other"] = a["tissue"] - a["bone"] - a["artifacts"] - a["hemato"] - a["adip"] - a["imv"]
    return a


def summary(px: dict, pixel_um: float, n_adip: int) -> dict:
    """Формулы MarrowQuant: 1 — Hm/(Hm+Ad), 2 — Hm/(T − B − Art); площади в мм²."""
    mm2 = pixel_um * pixel_um / 1e6
    marrow = px["tissue"] - px["bone"] - px["artifacts"]
    out = {f"{k}_mm2": round(v * mm2, 4) for k, v in px.items()}
    out["marrow_mm2"] = round(marrow * mm2, 4)
    out["cellularity_eq1_pct"] = round(100 * px["hemato"] / (px["hemato"] + px["adip"]), 2) if px["hemato"] + px["adip"] else None
    out["cellularity_eq2_pct"] = round(100 * px["hemato"] / marrow, 2) if marrow else None
    out["adiposity_pct"] = round(100 * px["adip"] / marrow, 2) if marrow else None
    out["imv_pct"] = round(100 * px["imv"] / marrow, 2) if marrow else None
    out["other_pct"] = round(100 * px["other"] / marrow, 2) if marrow else None
    out["adipocytes"] = n_adip
    out["warnings"] = []
    if out["other_pct"] is not None and out["other_pct"] >= 25:
        out["warnings"].append("прочее 25 % и больше: показать образец эксперту")
    return out


def run(slide, base_mpp: float, resolution: str | float, fragments: list[Fragment],
        params: cellularity.Params | None = None, masks_dir: Path | None = None, log=print) -> dict:
    """Расчёт по фрагментам и итог по стеклу (сумма площадей, КЛ-3)."""
    if resolution == ORIGINAL:
        factor = MQ_DOWNSAMPLE
    else:
        factor = float(resolution) / base_mpp
    pixel_um = base_mpp * factor
    total = dict.fromkeys(("tissue", "artifacts", "bone", "hemato", "adip", "imv", "other"), 0)
    total_adip = 0
    results = []
    for index, fragment in enumerate(fragments, 1):
        t0 = time.perf_counter()
        w, h = analysis_size(fragment.bbox, factor)
        rgb = read_area(slide, fragment.bbox, factor)
        tissue, art = fragment_masks(fragment, factor, (h, w))
        t_read = time.perf_counter() - t0
        res = cellularity.analyse(rgb, tissue, art, pixel_um, params)
        del rgb
        px = areas(res, tissue, art)
        for k in total:
            total[k] += px[k]
        total_adip += res.n_adip
        item = summary(px, pixel_um, res.n_adip)
        item.update(fragment=index, size_px=[w, h], read_s=round(t_read, 1),
                    total_s=round(time.perf_counter() - t0, 1))
        results.append(item)
        log(f"фрагмент {index}: {w}×{h}, ткани {item['tissue_mm2']:.2f} мм², "
            f"клеточность {item['cellularity_eq1_pct']} % / {item['cellularity_eq2_pct']} %, "
            f"{item['total_s']} с")
        if masks_dir is not None:
            save_class_map(masks_dir / f"fragment_{index}.png", res, tissue, art)
    return {
        "resolution": resolution, "pixel_um": round(pixel_um, 4),
        "algorithm": "MarrowQuant 2.0 (перенос, ImageJ 1.54f)",
        "fragments": results,
        "total": summary(total, pixel_um, total_adip) if results else None,
    }


# Цвета MarrowQuant (sendResultsToQuPath), прочее — серое (КЛ-6)
CLASS_COLORS = {"imv": (255, 58, 163), "adip": (255, 240, 60), "bone": (158, 237, 208),
                "hemato": (25, 14, 145), "art": (0, 0, 0), "other": (200, 200, 200)}


def save_class_map(path: Path, res: cellularity.Result, tissue: np.ndarray, art: np.ndarray) -> None:
    img = np.full(tissue.shape + (3,), 255, np.uint8)
    img[tissue] = CLASS_COLORS["other"]
    for name, mask in (("imv", res.imv), ("hemato", res.hemato), ("adip", res.adip), ("bone", res.bone)):
        img[mask] = CLASS_COLORS[name]
    img[art & tissue] = CLASS_COLORS["art"]
    path.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(path), cv2.cvtColor(img, cv2.COLOR_RGB2BGR))
