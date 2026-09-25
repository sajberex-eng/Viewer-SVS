"""Проверка переноса MarrowQuant 2.0 без сканов заказчика и без ImageJ.

Запуск:  .venv\\Scripts\\python.exe -X utf8 tests\\cellularity_test.py

Точное совпадение с ImageJ проверяется отдельно, эталоном на сервере
(tools/mq_reference/README.md). Здесь — то, что должно выполняться всегда:
правила ImageJ на простых фигурах, что разделение по частям равно разделению
целиком, что части не пересекаются, что формулы и итог по стеклу считаются
по сумме площадей, и что расчёт по скану читает область и контуры QuPath.
"""
from __future__ import annotations

import json
import math
import shutil
import sys
import tempfile
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from server import cellularity as c  # noqa: E402
from server import cellularity_job as job  # noqa: E402
from server import cellularity_slide as cs  # noqa: E402
from server import ij_watershed  # noqa: E402
from server.config import StorageConfig  # noqa: E402

failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    print(f"[{'OK' if condition else 'СБОЙ'}] {name} {detail}")
    if not condition:
        failures.append(name)


def synthetic_marrow(h: int = 600, w: int = 900, seed: int = 1) -> tuple[np.ndarray, np.ndarray]:
    """Картинка «как H&E»: зернистая кроветворная ткань (ядра и цитоплазма вперемешку),
    гладкая розовая балка кости и белые круглые жировые клетки."""
    rng = np.random.default_rng(seed)
    tissue = np.zeros((h, w), np.uint8)
    cv2.ellipse(tissue, (w // 2, h // 2), (w // 2 - 20, h // 2 - 20), 0, 0, 360, 1, -1)
    grain = rng.random((h, w))[..., None] < 0.8
    img = np.where(grain, np.array([80, 50, 140], np.uint8), np.array([200, 150, 200], np.uint8)).astype(np.uint8)
    cv2.rectangle(img, (80, 80), (w - 80, 130), (235, 120, 160), -1)
    fat = np.zeros((h, w), np.uint8)
    for _ in range(90):
        x, y = int(rng.integers(60, w - 60)), int(rng.integers(160, h - 60))
        cv2.circle(fat, (x, y), int(rng.integers(9, 18)), 1, -1)
    img[fat == 1] = (248, 246, 248)
    img[tissue == 0] = 245
    return img, tissue.astype(bool)


def main() -> None:
    # ---------- правила ImageJ на простых фигурах ----------
    square = np.ones((6, 6), np.uint8)
    check("периметр квадрата 6×6 как у ImageJ (24 − 4·(2 − √2))",
          abs(c.traced_perimeter(square) - (24 - 4 * (2 - math.sqrt(2)))) < 1e-9)
    disc = np.fromfunction(lambda y, x: (x - 20) ** 2 + (y - 20) ** 2 <= 100, (41, 41)).astype(np.uint8)
    check("большая ось эллипса у круга радиусом 10 — около 20", abs(c.ellipse_major(disc) - 20) < 0.5,
          f"({c.ellipse_major(disc):.3f})")
    check("двухуровневая гистограмма: порог — второй уровень минус один",
          c.auto_threshold(np.bincount([10] * 5 + [200] * 7, minlength=256), "MaxEntropy") == 199)
    ramp = np.bincount(np.r_[np.repeat(40, 500), np.repeat(180, 500), np.arange(256)], minlength=256)
    t = c.auto_threshold(ramp, "Default")
    check("порог Default между двумя пиками", 40 < t < 180, f"({t})")
    grown = c.dilate(np.pad(np.ones((1, 1), np.uint8), 3), 1, 1)
    check("dilate со счётом 1 — квадрат 3×3", int(grown.sum()) == 9)
    eroded = c.erode(np.ones((5, 5), np.uint8), 1, 1, pad_edges=True)
    check("erode с «pad»: за краем объект, картинка из единиц не меняется", int(eroded.sum()) == 25)
    eroded = c.erode(np.ones((5, 5), np.uint8), 1, 1, pad_edges=False)
    check("erode без «pad»: край съедается", int(eroded.sum()) == 9)
    sat = c.hsb_saturation(np.array([[[255, 0, 0], [128, 128, 128], [0, 0, 0]]], np.uint8))
    check("насыщенность как java.awt.Color", sat.tolist() == [[255, 0, 0]], f"({sat.tolist()})")

    # ---------- разделение слипшихся ----------
    two = np.zeros((60, 90), np.uint8)
    cv2.circle(two, (30, 30), 18, 1, -1)
    cv2.circle(two, (58, 30), 18, 1, -1)
    parts = cv2.connectedComponents(c.watershed(two), connectivity=8)[0] - 1
    check("два слипшихся круга разделяются на две частицы", parts == 2, f"({parts})")
    rng = np.random.default_rng(3)
    blobs = np.zeros((300, 400), np.uint8)
    for _ in range(60):
        cv2.circle(blobs, (int(rng.integers(0, 400)), int(rng.integers(0, 300))), int(rng.integers(6, 25)), 1, -1)
    dist = ij_watershed.edm(blobs)
    whole = (ij_watershed._find_maxima_segmented(dist.ravel(), 400, 300, np.float32(0), np.float32(dist.max()))
             .reshape(300, 400) == 255).astype(np.uint8)
    check("разделение по частям равно разделению всей картинки (и у краёв)",
          int((whole != ij_watershed.watershed(blobs)).sum()) == 0)
    edge = np.ones((20, 20), np.uint8)
    edge[10, 10] = 0
    check("край картинки фоном не считается (edgesAreBackground = false)",
          abs(float(ij_watershed.edm(edge)[0, 0]) - math.hypot(10, 10)) < 1e-4)

    # ---------- память (КЛ-7): части без карты меток, морфология полосами ----------
    ring = np.zeros((80, 80), np.uint8)
    cv2.circle(ring, (40, 40), 30, 1, 6)          # кольцо…
    cv2.circle(ring, (40, 40), 8, 1, -1)          # …и часть внутри его дыры
    cv2.rectangle(ring, (0, 0), (5, 5), 1, -1)    # часть у самого края
    n_ref, lab = cv2.connectedComponents(ring, connectivity=8)
    parts = list(ij_watershed.components(ring))
    rebuilt = np.zeros_like(ring)
    for x, y, w, h, part in parts:
        rebuilt[y:y + h, x:x + w] |= part
    single = all(len(np.unique(lab[y:y + h, x:x + w][part == 1])) == 1 for x, y, w, h, part in parts)
    check("части по контурам: те же, что по карте меток, включая часть внутри дыры",
          len(parts) == n_ref - 1 and np.array_equal(rebuilt, ring) and single,
          f"({len(parts)} и {n_ref - 1})")
    check("число частей без карты меток", ij_watershed.count_components(ring) == n_ref - 1)
    noise = (rng.random((300, 200)) < 0.4).astype(np.uint8)

    def plain(fg, iterations, step):
        for _ in range(iterations):
            fg = step(fg)
        return fg

    ref_d = plain(noise, 20, lambda b: b | (c._neighbours(b, 0) >= 2).view(np.uint8))
    ref_e = plain(noise, 20, lambda b: b & ((8 - c._neighbours(b, 1)) < 2).view(np.uint8))
    saved = c.STRIP
    c.STRIP = 16
    band_d, band_e = c.dilate(noise, 20, 2), c.erode(noise, 20, 2, pad_edges=True)
    c.STRIP = saved
    check("морфология полосами с полем равна расчёту целиком (dilate и erode по 20 итераций)",
          np.array_equal(band_d, ref_d) and np.array_equal(band_e, ref_e))

    # ---------- весь расчёт на синтетике ----------
    img, tissue = synthetic_marrow()
    art = np.zeros_like(tissue)
    cv2.circle(art.view(np.uint8), (450, 400), 30, 1, -1)
    res = c.analyse(img, tissue, art, 2.0, keep_debug=True)
    masks = {"кость": res.bone, "кроветворная": res.hemato, "жир": res.adip, "строма": res.imv}
    overlap = sum(int((a & b).sum()) for i, a in enumerate(masks.values()) for b in list(masks.values())[i + 1:])
    check("части не пересекаются", overlap == 0, f"({overlap})")
    outside = sum(int((m & ~tissue).sum()) + int((m & art).sum()) for m in masks.values())
    check("части не выходят за ткань и не заходят в артефакт", outside == 0, f"({outside})")
    check("на синтетике найдены кость, кроветворная ткань и жир",
          all(int(masks[k].sum()) > 0 for k in ("кость", "кроветворная", "жир")),
          "(" + ", ".join(f"{k} {int(m.sum())}" for k, m in masks.items()) + ")")
    check("жировые клетки найдены поштучно", res.n_adip > 20, f"({res.n_adip})")
    # Полосы: тот же расчёт с полосой в 7 строк должен дать то же самое
    saved = c.STRIP
    c.STRIP = 7
    res7 = c.analyse(img, tissue, art, 2.0)
    c.STRIP = saved
    same = all(np.array_equal(getattr(res, k), getattr(res7, k)) for k in ("bone", "hemato", "adip", "imv"))
    check("расчёт полосами не зависит от высоты полосы", same)

    # ---------- формулы и итог ----------
    px = {"tissue": 1000, "artifacts": 100, "bone": 200, "hemato": 300, "adip": 200, "imv": 50, "other": 150}
    s = cs.summary(px, 2.0, 7)
    check("формула 1: Hm / (Hm + Ad)", s["cellularity_eq1_pct"] == 60.0, f"({s['cellularity_eq1_pct']})")
    check("формула 2: Hm / (T − B − Art)", s["cellularity_eq2_pct"] == 42.86, f"({s['cellularity_eq2_pct']})")
    check("при прочем 21 % предупреждения нет", bool(s["warnings"]) is False and s["other_pct"] == 21.43)
    px["other"] = 300
    check("предупреждение при прочем от 25 %", bool(cs.summary(px, 2.0, 7)["warnings"]))

    # ---------- по скану: область, контуры QuPath, итог по сумме площадей ----------
    work = Path(tempfile.mkdtemp(prefix="viewer-cellularity-test-"))
    try:
        from synthetic_svs import build_svs
        import openslide

        path = build_svs(work / "m.svs", size=2048, objective=20, mpp=0.5)
        slide = openslide.OpenSlide(str(path))
        square_poly = [[[100, 100], [900, 100], [900, 900], [100, 900], [100, 100]]]
        rect_poly = [[[1100, 200], [1900, 200], [1900, 600], [1100, 600], [1100, 200]]]
        art_poly = [[[300, 300], [400, 300], [400, 400], [300, 400], [300, 300]]]
        geo = {"type": "FeatureCollection", "features": [
            {"type": "Feature", "geometry": {"type": "Polygon", "coordinates": square_poly},
             "properties": {"classification": {"name": "Tissue Boundaries"}}},
            {"type": "Feature", "geometry": {"type": "Polygon", "coordinates": rect_poly},
             "properties": {"classification": {"name": "Tissue Boundaries"}}},
            {"type": "Feature", "geometry": {"type": "Polygon", "coordinates": art_poly},
             "properties": {"classification": {"name": "Artifact"}}},
            # контур без класса: в QuPath так можно, разбор не должен падать
            {"type": "Feature", "geometry": {"type": "Polygon", "coordinates": rect_poly},
             "properties": {"objectType": "annotation"}},
        ]}
        (work / "c.geojson").write_text(json.dumps(geo), encoding="utf-8")
        fragments = cs.contours_from_geojson(work / "c.geojson")
        check("GeoJSON QuPath: два фрагмента, артефакт у первого",
              len(fragments) == 2 and len(fragments[0].artifacts) == 1 and not fragments[1].artifacts)
        (work / "list.geojson").write_text(json.dumps(geo["features"]), encoding="utf-8")
        check("GeoJSON списком, без FeatureCollection, читается так же",
              len(cs.contours_from_geojson(work / "list.geojson")) == 2)
        rows = cs.SlideRows(slide, fragments[0].bbox, 4.0)
        whole = np.asarray(rows)
        check("строки области со скана по требованию совпадают с областью целиком (через край полосы)",
              whole.shape == (200, 200, 3) and np.array_equal(rows[0:7], whole[0:7])
              and np.array_equal(rows[190:200], whole[190:200]),
              f"({whole.shape})")
        big = cs.SlideRows(slide, (0, 0, 2048, 2048), 1.0)
        check("полоса на границе двух чтений склеивается верно",
              np.array_equal(big[big.rows - 3:big.rows + 3], np.asarray(big)[big.rows - 3:big.rows + 3]))
        saved_budget = cs.READ_BUDGET_PX
        cs.READ_BUDGET_PX = 2048 * 64                  # чтение не больше 64 строк уровня 0
        small = cs.SlideRows(slide, (0, 0, 2048, 2048), 1.0)
        cs.READ_BUDGET_PX = saved_budget
        check("широкая область читается меньшими полосами, результат тот же",
              small.rows == 64 and small.keep >= (c.STRIP + 2) // 64 + 1
              and np.array_equal(np.asarray(small), np.asarray(big)) and np.array_equal(small[250:270], np.asarray(big)[250:270]),
              f"(полоса {small.rows} строк, в кэше {small.keep})")
        result = cs.run(slide, 0.5, cs.ORIGINAL, fragments, log=lambda *_: None)
        check("разрешение оригинала: уменьшение в 4 раза", result["pixel_um"] == 2.0, f"({result['pixel_um']})")
        f1 = result["fragments"][0]
        check("площадь фрагмента по контуру: 800 × 800 точек по 0,5 мкм = 0,16 мм²",
              abs(f1["tissue_mm2"] - 0.16) < 1e-9, f"({f1['tissue_mm2']})")
        check("площадь артефакта: 100 × 100 точек = 0,0025 мм²",
              abs(f1["artifacts_mm2"] - 0.0025) < 1e-9, f"({f1['artifacts_mm2']})")
        tot = result["total"]
        check("итог по стеклу — сумма площадей фрагментов",
              abs(tot["tissue_mm2"] - sum(f["tissue_mm2"] for f in result["fragments"])) < 1e-3)
        r1 = cs.run(slide, 0.5, 1.0, fragments[:1], log=lambda *_: None)
        check("разрешение 1 мкм: размер области 400 × 400", r1["fragments"][0]["size_px"] == [400, 400],
              f"({r1['fragments'][0]['size_px']})")
        auto = cs.contours_auto(slide, 0.5)
        check("поиск фрагментов по миниатюре не падает", isinstance(auto, list), f"({len(auto)})")
        slide.close()

        # ---------- шаг 3: контуры из аннотаций, полигон по миниатюре, карты классов, сетка тайлов ----------
        from server.tiles import DeepZoomGrid
        frags = cs.fragments_from_polygons([[square_poly[0]], [rect_poly[0]]], [[art_poly[0]]])
        check("фрагменты из контуров вьювера: артефакт попал в свой фрагмент",
              len(frags) == 2 and len(frags[0].artifacts) == 1 and not frags[1].artifacts
              and frags[0].bbox == (100, 100, 900, 900))
        thumb = np.zeros((40, 60), np.uint8)
        cv2.circle(thumb, (30, 20), 15, 1, -1)
        found = cs.Fragment(bbox=(1000, 2000, 1000 + 60 * 16, 2000 + 40 * 16), thumb_mask=thumb, thumb_ds=16.0)
        poly = cs.fragment_polygon(found)
        xs, ys = [p[0] for p in poly], [p[1] for p in poly]
        check("контур по миниатюре: замкнутый многоугольник в точках уровня 0 вокруг круга",
              8 <= len(poly) <= 80 and abs(min(xs) - (1000 + 15 * 16)) < 32 and abs(max(xs) - (1000 + 45 * 16)) < 32
              and abs(min(ys) - (2000 + 5 * 16)) < 32, f"({len(poly)} вершин, x {min(xs):.0f}…{max(xs):.0f})")
        cmap = cs.class_map(res, tissue, art)
        cs.save_class_map(work / "map.png", res, tissue, art)
        back = cs.load_class_map(work / "map.png")
        check("карта классов: PNG с палитрой читается обратно теми же индексами",
              np.array_equal(cmap, back) and set(np.unique(cmap)) <= set(range(7)) and (cmap == 6).sum() == int((art & tissue).sum()))
        # ---------- оценка ИИ по полям зрения (шаг 6): выбор полей и сведение без сети ----------
        from server import cellularity_ai as ai
        disc_mask = np.zeros((120, 120), np.uint8)
        cv2.circle(disc_mask, (60, 60), 50, 1, -1)
        fragment_ai = cs.Fragment(bbox=(0, 0, 120 * 32, 120 * 32), thumb_mask=disc_mask, thumb_ds=32.0)
        fields = ai.sample_fields(fragment_ai, 0.5, 4)   # диск диаметром 3,2 мм, участок 0,8 мм
        inside = all(0 <= f["x"] and f["x"] + f["size"] <= 120 * 32 and f["tissue_fraction"] >= 0.6 for f in fields)
        apart = all(math.hypot(a["x"] - b["x"], a["y"] - b["y"]) >= a["size"] * 0.45
                    for i, a in enumerate(fields) for b in fields[i + 1:])  # шаг сетки — половина окна
        check("участки ИИ без модели: внутри ткани, с долей ткани не меньше 0,6, разнесены",
              2 <= len(fields) <= 4 and inside and apart and fields[0]["size"] == 1600, f"({len(fields)} участков)")
        # точки модели (пиксели обзора) привязываются к ближайшему окну с тканью, дубли не берутся
        snapped = ai.snap_regions(fragment_ai, 0.5, [{"x": 60, "y": 60, "reason": "центр"}, {"x": 61, "y": 60, "reason": "тот же"}], 32.0)
        check("привязка участков: центр диска → окно вокруг центра, второй участок не совпадает с первым",
              len(snapped) == 2 and abs(snapped[0]["x"] + 800 - 60 * 32) < 300 and abs(snapped[0]["y"] + 800 - 60 * 32) < 300
              and snapped[0]["reason"] == "центр" and (snapped[1]["x"], snapped[1]["y"]) != (snapped[0]["x"], snapped[0]["y"])
              and [r["n"] for r in snapped] == [1, 2], f"({snapped})")
        check("площадь ткани для веса фрагмента ~ площадь диска",
              abs(ai.tissue_area_mm2(fragment_ai, 0.5) - math.pi * 0.8 ** 2) < 0.15)
        answers = iter([{"cellularity_percent": 40, "regional_min_percent": 30, "regional_max_percent": 50,
                         "heterogeneous": False, "causes": ["жировые поля"], "note": "", "_usage": {"input_tokens": 9000, "output_tokens": 100}},
                        {"cellularity_percent": 70, "regional_min_percent": 40, "regional_max_percent": 90,
                         "heterogeneous": True, "causes": ["лимфоидный узелок"], "note": " слева  плотнее ", "_usage": {"input_tokens": 9500, "output_tokens": 110}}])
        calls = []

        def fake_ask(client, model, images, text, schema):
            calls.append((schema.__name__, len(images), all(i[:2] == b"\xff\xd8" for i in images), text))
            if schema is ai.RegionChoice:
                return {"regions": [{"x": 10, "y": 10, "reason": "край"}, {"x": 100, "y": 100, "reason": "центр"}],
                        "_usage": {"input_tokens": 1000, "output_tokens": 50}}
            return next(answers)

        # два квадрата 2 × 2 мм: в каждом помещаются два поля по 1 мм
        two = [cs.Fragment(bbox=(0, 0, 4000, 4000), tissue=[[[0, 0], [4000, 0], [4000, 4000], [0, 4000]]]),
               cs.Fragment(bbox=(0, 0, 4000, 4000), tissue=[[[0, 0], [4000, 0], [4000, 4000], [0, 4000]]])]
        progress = []
        with openslide.OpenSlide(str(path)) as reopened:   # скан выше уже закрыт
            out = ai.estimate(reopened, 0.5, two, None, "test-model", progress=lambda i, n, s: progress.append((i, n, s)), ask=fake_ask)
        check("оценка ИИ: стекло — среднее фрагментов с весом площади, разброс — крайние по фрагментам, описание сохранено",
              out["fragments"][0]["cellularity_eq1_pct"] == 40.0 and out["fragments"][1]["cellularity_eq1_pct"] == 70.0
              and out["total"]["cellularity_eq1_pct"] == 55.0 and out["total"]["fields_used"] == 4
              and out["total"]["regional_min_pct"] == 30.0 and out["total"]["regional_max_pct"] == 90.0
              and out["total"]["heterogeneous"] is True and out["fragments"][1]["description"] == "слева плотнее"
              and out["fragments"][1]["causes"] == ["лимфоидный узелок"] and out["fragments"][0]["causes"] == []
              and out["fragments"][0]["regions"][0]["reason"] == "край" and out["method"] == "ai"
              and out["usage"] == {"requests": 4, "input_tokens": 20500, "output_tokens": 310},
              f"({[f['cellularity_eq1_pct'] for f in out['fragments']]}, итог {out['total']})")
        check("на фрагмент два запроса: обзор (1 картинка) и оценка (обзор + 2 участка, JPEG), в тексте — номер фрагмента",
              [c[:3] for c in calls] == [("RegionChoice", 1, True), ("FragmentEstimate", 3, True)] * 2
              and "фрагмента 2 из 2" in calls[3][3] and "участки ×20" in calls[1][3], f"({[c[:3] for c in calls]})")
        check("ход оценки ИИ — по фрагментам и шагам",
              progress == [(1, 2, "pick"), (1, 2, "estimate"), (2, 2, "pick"), (2, 2, "estimate")], f"({progress})")
        grid = DeepZoomGrid(2048, 2048, 510, 1)
        check("сетка DeepZoom: 12 уровней, тайл с полями",
              grid.level_count == 12 and grid.tile_box(11, 1, 0) == (509, 0, 1021, 511) and grid.tile_box(11, 0, 0) == (0, 0, 511, 511))

        # ---------- отдельный процесс (КЛ-7) ----------
        est = cs.estimate_peak_mb(fragments, 0.5, 1.0)
        check("оценка памяти: библиотеки плюс байты на точку крупнейшего фрагмента",
              abs(est - (150 + c.TRANSIENT_MB + 400 * 400 * c.PEAK_BYTES_PER_PIXEL / 2 ** 20)) < 1e-6, f"({est:.1f} МБ)")
        storage = StorageConfig(root=str(work))
        spec = job.JobSpec(storage, "m.svs", 0.5, cs.ORIGINAL, fragments)
        seen = []
        out = job.run_in_process(spec, on_progress=seen.append)
        same = all(out["fragments"][i][k] == result["fragments"][i][k]
                   for i in range(2) for k in ("tissue_mm2", "cellularity_eq1_pct", "adipocytes"))
        check("расчёт в отдельном процессе даёт тот же результат", same)
        check("ход расчёта приходит по стадиям, доля растёт до конца",
              len(seen) == 2 * len(c.STAGES) and seen[0].fraction == 0
              and all(a.fraction <= b.fraction for a, b in zip(seen, seen[1:])) and seen[-1].fraction < 1,
              f"({len(seen)} сообщений)")
        check("пик памяти процесса измерен, память по стадиям приходит с ходом",
              out["peak_rss_mb"] > 20 and out["elapsed_s"] > 0 and all(p.rss_mb > 20 for p in seen),
              f"({out['peak_rss_mb']} МБ, {out['elapsed_s']} с)")
        check("высота полосы ограничена числом точек", c.strip_rows(1000) == 256 and c.strip_rows(8000) == 125
              and c.strip_rows(10 ** 9) == 16)
        try:
            job.run_in_process(job.JobSpec(storage, "m.svs", 0.5, 1.0, fragments, memory_limit_mb=1))
            check("отказ по оценке памяти до запуска процесса", False)
        except job.JobError as exc:
            check("отказ по оценке памяти до запуска процесса", "разрешение" in str(exc), f"({exc})")
        cancel = __import__("threading").Event()
        cancel.set()
        try:
            job.run_in_process(spec, cancel=cancel)
            check("отмена убивает процесс", False)
        except job.JobCancelled:
            check("отмена убивает процесс", True)
        try:
            job.run_in_process(job.JobSpec(storage, "m.svs", 0.5, cs.ORIGINAL, fragments, time_limit_s=0.01))
            check("предел времени останавливает расчёт", False)
        except job.JobError as exc:
            check("предел времени останавливает расчёт", "времени" in str(exc), f"({exc})")
        try:
            job.run_in_process(job.JobSpec(storage, "нет.svs", 0.5, cs.ORIGINAL, fragments))
            check("ошибка в процессе доходит текстом", False)
        except job.JobError as exc:
            check("ошибка в процессе доходит текстом", "ошибка расчёта" in str(exc))
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    main()
    if failures:
        print(f"\nНе пройдено: {len(failures)}")
        for name in failures:
            print(f"  - {name}")
        raise SystemExit(1)
    print("\nВсе проверки клеточности пройдены")
