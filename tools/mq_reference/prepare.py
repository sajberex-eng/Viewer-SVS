"""Подготовка области скана для сверки с эталоном (tools/mq_reference/README.md).

Находит фрагменты ткани по миниатюре, берёт прямоугольник вокруг выбранного фрагмента
в разрешении оригинала (уменьшение в 4 раза, как в MarrowQuant2.0.groovy) и пишет в
папку rgb.png, tissue.png и art.png (артефакт — эллипс внутри ткани, чтобы проверить и
эту ветку). Запускается в образе вьювера, файлы не покидают сервер.

    python prepare.py <файл скана> <папка> [номер фрагмента] [уменьшение]
"""
import sys
from pathlib import Path

import cv2
import numpy as np
import openslide


def fragments(slide):
    base_mpp = float(slide.properties["openslide.mpp-x"])
    level = slide.get_best_level_for_downsample(16 / base_mpp)
    ds = slide.level_downsamples[level]
    thumb = np.asarray(slide.read_region((0, 0), level, slide.level_dimensions[level]).convert("RGB"))
    hsv = cv2.cvtColor(thumb, cv2.COLOR_RGB2HSV_FULL)
    mask = ((hsv[..., 1] > 25) & (hsv[..., 2] > 40)).astype(np.uint8)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8))
    flood = mask.copy()
    h, w = mask.shape
    cv2.floodFill(flood, np.zeros((h + 2, w + 2), np.uint8), (0, 0), 1)
    mask = mask | (1 - flood)
    n, lab, stats, _ = cv2.connectedComponentsWithStats(mask)
    um2 = (ds * base_mpp) ** 2
    found = [k for k in range(1, n) if stats[k, 4] * um2 >= 0.5e6]
    found.sort(key=lambda k: -stats[k, 4])
    return [(lab == k).astype(np.uint8) for k in found], ds


def main():
    path, out = sys.argv[1], Path(sys.argv[2])
    index = int(sys.argv[3]) if len(sys.argv) > 3 else 0
    downsample = float(sys.argv[4]) if len(sys.argv) > 4 else 4.0
    out.mkdir(parents=True, exist_ok=True)
    slide = openslide.OpenSlide(path)
    masks, ds = fragments(slide)
    thumb_mask = masks[index]
    ys, xs = np.nonzero(thumb_mask)
    # прямоугольник фрагмента в точках уровня 0 и в точках расчёта
    x0, y0 = int(xs.min() * ds), int(ys.min() * ds)
    x1, y1 = int((xs.max() + 1) * ds), int((ys.max() + 1) * ds)
    w, h = int(round((x1 - x0) / downsample)), int(round((y1 - y0) / downsample))
    level = max(i for i, d in enumerate(slide.level_downsamples) if d <= downsample * 1.02)
    lds = slide.level_downsamples[level]
    region = slide.read_region((x0, y0), level, (int(round((x1 - x0) / lds)), int(round((y1 - y0) / lds))))
    rgba = np.asarray(region)
    rgb = rgba[..., :3].copy()
    rgb[rgba[..., 3] == 0] = 255
    rgb = cv2.resize(rgb, (w, h), interpolation=cv2.INTER_AREA)
    crop = thumb_mask[ys.min():ys.max() + 1, xs.min():xs.max() + 1]
    tissue = cv2.resize(crop * 255, (w, h), interpolation=cv2.INTER_NEAREST)
    # артефакт: эллипс у точки ткани, ближайшей к центру
    ty, tx = np.nonzero(tissue)
    c = np.argmin((tx - tx.mean()) ** 2 + (ty - ty.mean()) ** 2)
    art = np.zeros_like(tissue)
    cv2.ellipse(art, (int(tx[c]), int(ty[c])), (max(w // 30, 8), max(h // 30, 6)), 0, 0, 360, 255, -1)
    art &= tissue
    cv2.imwrite(str(out / "rgb.png"), cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR))
    cv2.imwrite(str(out / "tissue.png"), tissue)
    cv2.imwrite(str(out / "art.png"), art)
    pixel_um = float(slide.properties["openslide.mpp-x"]) * downsample
    (out / "pixel_um.txt").write_text(f"{pixel_um}\n")
    print(f"фрагментов {len(masks)}, взят {index}: {w}×{h} точек по {pixel_um:.4f} мкм, "
          f"ткани {int((tissue > 0).sum())} точек")


if __name__ == "__main__":
    main()
