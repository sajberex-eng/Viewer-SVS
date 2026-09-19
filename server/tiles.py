"""Нарезка слайда на тайлы по схеме DeepZoom.

Свой генератор вместо openslide.deepzoom.DeepZoomGenerator: у Aperio SVS шаг уровней
пирамиды чуть больше целого (4,0001 вместо 4), и стандартный выбор уровня
«не грубее запрошенного» из-за этого читает полное разрешение и сжимает его в 4 раза.
Здесь уровень выбирается с допуском, и тайл 10× читается напрямую из уровня 10×.
"""
from __future__ import annotations

import math

import openslide
from PIL import Image

LEVEL_TOLERANCE = 1.02  # уровень считается подходящим, если он грубее нужного не более чем на 2 %


class DeepZoomTiler:
    def __init__(self, slide: openslide.OpenSlide, tile_size: int, overlap: int):
        self._slide = slide
        self._tile_size = tile_size
        self._overlap = overlap
        self._background = "#" + slide.properties.get(openslide.PROPERTY_NAME_BACKGROUND_COLOR, "ffffff")
        width, height = slide.dimensions
        self.level_count = math.ceil(math.log2(max(width, height))) + 1
        # Уровень DeepZoom l уменьшен относительно полного разрешения в 2^(level_count - 1 - l) раз.
        self.level_dimensions = [
            (math.ceil(width / self._scale(level)), math.ceil(height / self._scale(level)))
            for level in range(self.level_count)
        ]

    def _scale(self, level: int) -> int:
        return 2 ** (self.level_count - 1 - level)

    def tile_count(self, level: int) -> tuple[int, int]:
        width, height = self.level_dimensions[level]
        return math.ceil(width / self._tile_size), math.ceil(height / self._tile_size)

    def get_tile(self, level: int, col: int, row: int) -> Image.Image:
        """Тайл в формате RGB. Границы level/col/row проверяет вызывающий код."""
        level_width, level_height = self.level_dimensions[level]
        left = max(col * self._tile_size - self._overlap, 0)
        top = max(row * self._tile_size - self._overlap, 0)
        right = min((col + 1) * self._tile_size + self._overlap, level_width)
        bottom = min((row + 1) * self._tile_size + self._overlap, level_height)
        tile_size = (right - left, bottom - top)

        scale = self._scale(level)
        native = self._native_level(scale)
        ratio = scale / self._slide.level_downsamples[native]
        native_width, native_height = self._slide.level_dimensions[native]
        read_size = (
            max(1, min(math.ceil(tile_size[0] * ratio), native_width - int(left * ratio))),
            max(1, min(math.ceil(tile_size[1] * ratio), native_height - int(top * ratio))),
        )
        region = self._slide.read_region((left * scale, top * scale), native, read_size)

        tile = Image.new("RGB", region.size, self._background)
        tile.paste(region, mask=region.getchannel("A"))
        if tile.size != tile_size:
            tile = tile.resize(tile_size, Image.Resampling.BILINEAR)
        return tile

    def _native_level(self, scale: float) -> int:
        downsamples = self._slide.level_downsamples
        suitable = [i for i, downsample in enumerate(downsamples) if downsample <= scale * LEVEL_TOLERANCE]
        return max(suitable, key=lambda i: downsamples[i]) if suitable else 0
