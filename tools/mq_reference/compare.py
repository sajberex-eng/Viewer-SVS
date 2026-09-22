"""Сверка переноса MarrowQuant с эталоном ImageJ по точкам (tools/mq_reference/README.md).

Считает server/cellularity.py на тех же rgb.png, tissue.png, art.png, что и
MQReference.java, и сравнивает каждую маску: число точек, совпадение (IoU) и число
несовпавших точек, плюс промежуточные маски жировых кандидатов.

    python compare.py <папка> [adipMin adipMax minCir]
"""
import json
import sys
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from server import cellularity  # noqa: E402


def load(path):
    img = cv2.imread(str(path), cv2.IMREAD_UNCHANGED)
    return None if img is None else img > 127


def main():
    d = Path(sys.argv[1])
    p = cellularity.Params()
    if len(sys.argv) > 4:
        p.adip_min_um2, p.adip_max_um2, p.min_circularity = map(float, sys.argv[2:5])
    rgb = cv2.cvtColor(cv2.imread(str(d / "rgb.png")), cv2.COLOR_BGR2RGB)
    tissue = load(d / "tissue.png")
    art = load(d / "art.png") if (d / "art.png").exists() else None
    pixel_um = float((d / "pixel_um.txt").read_text())
    res = cellularity.analyse(rgb, tissue, art, pixel_um, p, keep_debug=True)
    ref = json.loads((d / "ref.json").read_text())
    print("пороги:  наш / ImageJ")
    for key in ("bone_min", "bone_max", "bone_lower", "var_min", "var_max", "var_lower", "hem_lower"):
        print(f"  {key:12s} {res.debug[key]!r:>24} {ref.get(key)!r:>24}")
    print("частицы: наш / ImageJ")
    for key in ("particles_after_size_circ", "particles_after_round"):
        print(f"  {key:26s} {res.debug[key]:>8} {ref.get(key):>8}")
    print(f"  жировых клеток             {res.n_adip:>8} {ref['n_adip']:>8}")
    print("маски: точек наш / ImageJ, IoU, не совпало")
    pairs = [("bone", res.bone), ("imv", res.imv), ("hemato", res.hemato), ("adip", res.adip),
             ("adip_raw", res.debug["adip_raw"]), ("adip_ws", res.debug["adip_ws"]),
             ("candidates", res.debug["candidates"])]
    for name, ours in pairs:
        theirs = load(d / f"ref_{name}.png")
        ours = ours.astype(bool)
        inter = (ours & theirs).sum()
        union = (ours | theirs).sum()
        diff = (ours ^ theirs).sum()
        print(f"  {name:10s} {int(ours.sum()):>10} {int(theirs.sum()):>10}  "
              f"{(inter / union if union else 1.0):.5f}  {int(diff):>8}")
        if diff:
            vis = np.zeros(ours.shape + (3,), np.uint8)
            vis[ours & theirs] = (200, 200, 200)
            vis[ours & ~theirs] = (0, 0, 255)      # только у нас — красное
            vis[~ours & theirs] = (255, 0, 0)      # только у ImageJ — синее
            cv2.imwrite(str(d / f"diff_{name}.png"), vis)
    tissue_px = int(tissue.sum())
    marrow = tissue_px - int(res.bone.sum()) - int((art & tissue).sum() if art is not None else 0)
    for label, r in (("наш", {k: int(getattr(res, k).sum()) for k in ("hemato", "adip")}),
                     ("ImageJ", {"hemato": ref["hemato_px"], "adip": ref["adip_px"]})):
        eq1 = 100 * r["hemato"] / max(1, r["hemato"] + r["adip"])
        eq2 = 100 * r["hemato"] / max(1, marrow)
        print(f"клеточность {label:6s}: формула 1 {eq1:.2f} %, формула 2 {eq2:.2f} %")


if __name__ == "__main__":
    main()
