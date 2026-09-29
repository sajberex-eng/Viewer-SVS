"""Таблица цвета из ICC-профиля сканера («Цвет как в сканере», раздел 7 ТЗ, этап 12).

Сканеры Aperio записывают в файл профиль своей камеры; ImageScope применяет его
при включённом управлении цветом. Здесь профиль один раз переводится в трёхмерную
таблицу (LUT_SIZE³ узлов) «цвет в файле → sRGB», которую шейдер в браузере
применяет первым шагом. Таблица упакована в PNG: LUT_SIZE срезов по синему
каналу в ряд, внутри среза x — красный, y — зелёный.
"""
from __future__ import annotations

import os
from pathlib import Path

import numpy as np
from PIL import Image, ImageCms

LUT_SIZE = 33


def lut_grid(size: int = LUT_SIZE) -> Image.Image:
    """Картинка с узлами таблицы в исходных цветах: пиксель (x = b·size + r, y = g) = (r, g, b)."""
    levels = np.linspace(0, 255, size).round().astype(np.uint8)
    x = np.arange(size * size)
    grid = np.zeros((size, size * size, 3), np.uint8)
    grid[..., 0] = levels[x % size][None, :]
    grid[..., 1] = levels[:, None]
    grid[..., 2] = levels[x // size][None, :]
    return Image.fromarray(grid, "RGB")


def build_color_lut(profile, size: int = LUT_SIZE) -> Image.Image:
    """Таблица «профиль сканера → sRGB». profile — ImageCmsProfile из OpenSlide."""
    srgb = ImageCms.createProfile("sRGB")
    transform = ImageCms.buildTransform(profile, srgb, "RGB", "RGB", renderingIntent=ImageCms.Intent.PERCEPTUAL)
    return ImageCms.applyTransform(lut_grid(size), transform)


def slide_profile(slide):
    """Профиль из файла или None: у KFB и у сканов без профиля его нет."""
    try:
        return getattr(slide, "color_profile", None)
    except Exception:  # noqa: BLE001 — повреждённый профиль равен отсутствующему
        return None


def lut_path(luts_dir: Path, slide_row) -> Path:
    """Имя включает дату файла: после замены скана таблица строится заново."""
    return luts_dir / f"{slide_row['id']}-{int(slide_row['mtime'])}.png"


def none_marker(path: Path) -> Path:
    """Отметка «профиля нет»: чтобы не открывать скан на каждый запрос."""
    return path.with_suffix(".none")


def render_color_lut(profile, path: Path) -> None:
    """Запись через временный файл: страница не получит недописанный PNG."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    build_color_lut(profile).save(tmp, "PNG")
    os.replace(tmp, path)
