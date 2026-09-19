"""Минимальный настоящий SVS для тестов: тайловый TIFF с JPEG и описанием Aperio.

OpenSlide опознаёт такой файл как Aperio, читает размер пикселя и увеличение и
отдаёт тайлы. Реальные сканы для тестов не нужны и не копируются.
"""
from __future__ import annotations

import io
import struct
from pathlib import Path

from PIL import Image

TAG_WIDTH, TAG_LENGTH, TAG_BITS, TAG_COMPRESSION, TAG_PHOTOMETRIC = 256, 257, 258, 259, 262
TAG_DESCRIPTION, TAG_SAMPLES, TAG_PLANAR = 270, 277, 284
TAG_TILE_W, TAG_TILE_L, TAG_TILE_OFFSETS, TAG_TILE_COUNTS, TAG_YCC_SUBSAMPLING = 322, 323, 324, 325, 530
SHORT, LONG, ASCII = 3, 4, 2


def _tile_jpeg(size: int, shade: int) -> bytes:
    image = Image.new("RGB", (size, size), (shade, 90 + shade // 3, 170))
    buffer = io.BytesIO()
    image.save(buffer, "JPEG", quality=70, subsampling=0)
    return buffer.getvalue()


def build_svs(path: Path, size: int = 512, tile: int = 256, objective: int = 20, mpp: float = 0.5) -> Path:
    tiles_per_side = size // tile
    jpegs = [_tile_jpeg(tile, 40 + 30 * i) for i in range(tiles_per_side * tiles_per_side)]
    description = (
        f"Aperio Image Library v11.2.1\r\n{size}x{size} [0,0 {size}x{size}] ({tile}x{tile}) JPEG/RGB Q=70"
        f"|AppMag = {objective}|MPP = {mpp}"
    ).encode("ascii") + b"\x00"

    body = bytearray()  # всё, что лежит после заголовка и до таблицы тегов
    base = 8

    def put(data: bytes) -> int:
        offset = base + len(body)
        body.extend(data)
        if len(body) % 2:
            body.append(0)  # значения в TIFF выравниваются по слову
        return offset

    tile_offsets = [put(j) for j in jpegs]
    tile_counts = [len(j) for j in jpegs]
    bits_offset = put(struct.pack("<3H", 8, 8, 8))
    desc_offset = put(description)
    offsets_offset = put(struct.pack(f"<{len(tile_offsets)}I", *tile_offsets))
    counts_offset = put(struct.pack(f"<{len(tile_counts)}I", *tile_counts))

    n_tiles = len(jpegs)
    entries = [
        (TAG_WIDTH, LONG, 1, size),
        (TAG_LENGTH, LONG, 1, size),
        (TAG_BITS, SHORT, 3, bits_offset),
        (TAG_COMPRESSION, SHORT, 1, 7),
        (TAG_PHOTOMETRIC, SHORT, 1, 6),
        (TAG_DESCRIPTION, ASCII, len(description), desc_offset),
        (TAG_SAMPLES, SHORT, 1, 3),
        (TAG_PLANAR, SHORT, 1, 1),
        (TAG_TILE_W, SHORT, 1, tile),
        (TAG_TILE_L, SHORT, 1, tile),
        (TAG_TILE_OFFSETS, LONG, n_tiles, offsets_offset),
        (TAG_TILE_COUNTS, LONG, n_tiles, counts_offset),
        (TAG_YCC_SUBSAMPLING, SHORT, 2, 1 | (1 << 16)),
    ]
    ifd_offset = base + len(body)
    ifd = bytearray(struct.pack("<H", len(entries)))
    for tag, kind, count, value in entries:
        ifd += struct.pack("<HHII", tag, kind, count, value)
    ifd += struct.pack("<I", 0)  # следующего каталога нет

    header = b"II" + struct.pack("<HI", 42, ifd_offset)
    path.write_bytes(header + bytes(body) + bytes(ifd))
    return path


if __name__ == "__main__":
    import sys

    import openslide

    target = build_svs(Path(sys.argv[1] if len(sys.argv) > 1 else "synthetic.svs"))
    with openslide.OpenSlide(str(target)) as slide:
        print("vendor:", slide.properties.get("openslide.vendor"))
        print("размеры:", slide.dimensions, "уровней:", slide.level_count)
        print("объектив:", slide.properties.get("openslide.objective-power"), "mpp:", slide.properties.get("openslide.mpp-x"))
        print("ассоциированные:", list(slide.associated_images))
