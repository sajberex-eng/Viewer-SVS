"""Проверка шагов переноса по отдельности: вход каждого шага берётся из эталона ImageJ.

Так ошибка одного шага не маскирует и не размножает ошибки следующих.

    python steps.py <папка>
"""
import sys
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from server import cellularity as c  # noqa: E402


def load(d, name):
    img = cv2.imread(str(d / name), cv2.IMREAD_UNCHANGED)
    return img > 127


def report(name, ours, theirs):
    ours, theirs = ours.astype(bool), theirs.astype(bool)
    diff = int((ours ^ theirs).sum())
    print(f"  {name:28s} наш {int(ours.sum()):>9}  ImageJ {int(theirs.sum()):>9}  не совпало {diff:>8}")


def main():
    d = Path(sys.argv[1])
    rgb = cv2.cvtColor(cv2.imread(str(d / "rgb.png")), cv2.COLOR_BGR2RGB)
    d0, d1, d2 = c.deconvolve(rgb)
    ref_var = cv2.imread(str(d / "ref_var.tif"), cv2.IMREAD_UNCHANGED)
    ours_var = c.variance3(c.sub8(d0, d1))
    delta = np.abs(ref_var.astype(np.float64) - ours_var)
    print(f"дисперсия: ImageJ {ref_var.min():.3f}…{ref_var.max():.3f}, наша {ours_var.min():.3f}…{ours_var.max():.3f}, "
          f"расхождение до {delta.max():.4f}, точек с расхождением > 0,01: {int((delta > 0.01).sum())}")
    ref_edm = cv2.imread(str(d / "ref_edm.tif"), cv2.IMREAD_UNCHANGED)
    from server import ij_watershed
    ours_edm = ij_watershed.edm(load(d, "ref_adip_raw.png"))
    print(f"карта расстояний: расхождение до {np.abs(ref_edm - ours_edm).max():.6f}, "
          f"точек с расхождением: {int((ref_edm != ours_edm).sum())}")
    import time
    t = time.perf_counter()
    ws = c.watershed(load(d, "ref_adip_raw.png"))
    print(f"разделение заняло {time.perf_counter() - t:.1f} с (вместе с компиляцией)")
    report("разделение (вход ImageJ)", ws, load(d, "ref_adip_ws.png"))
    cand = (1 - c.dilate(1 - load(d, "ref_adip_ws.png").astype(np.uint8), 50, 5))
    report("сужение (вход ImageJ)", cand, load(d, "ref_candidates.png"))
    pixel_um = float((d / "pixel_um.txt").read_text())
    adip, st = c.filter_particles(load(d, "ref_candidates.png"), pixel_um, c.Params())
    report("частицы (вход ImageJ)", adip, load(d, "ref_adip.png"))
    print("  ", st)


if __name__ == "__main__":
    main()
