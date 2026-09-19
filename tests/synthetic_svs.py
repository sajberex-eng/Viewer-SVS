"""Минимальный настоящий SVS для тестов: тайловый TIFF с JPEG и описанием Aperio.

OpenSlide опознаёт такой файл как Aperio, читает размер пикселя и увеличение и
отдаёт тайлы. Реальные сканы для тестов не нужны и не копируются.

`with_label=True` добавляет миниатюру, второй уровень пирамиды и этикетку.
Этикетку OpenSlide опознаёт по тегу NewSubfileType = 1, как в настоящем SVS.
"""
from __future__ import annotations

import io
import struct
from pathlib import Path

from PIL import Image

TAG_SUBFILE = 254  # NewSubfileType: по нему OpenSlide отличает этикетку (1) от миниатюры (0)
TAG_WIDTH, TAG_LENGTH, TAG_BITS, TAG_COMPRESSION, TAG_PHOTOMETRIC = 256, 257, 258, 259, 262
TAG_DESCRIPTION, TAG_STRIP_OFFSETS, TAG_SAMPLES, TAG_ROWS_PER_STRIP = 270, 273, 277, 278
TAG_STRIP_COUNTS, TAG_PLANAR = 279, 284
TAG_TILE_W, TAG_TILE_L, TAG_TILE_OFFSETS, TAG_TILE_COUNTS, TAG_YCC_SUBSAMPLING = 322, 323, 324, 325, 530
SHORT, LONG, ASCII = 3, 4, 2


def _tile_jpeg(size: int, shade: int) -> bytes:
    image = Image.new("RGB", (size, size), (shade, 90 + shade // 3, 170))
    buffer = io.BytesIO()
    image.save(buffer, "JPEG", quality=70, subsampling=0)
    return buffer.getvalue()


def _label_jpeg(width: int, height: int) -> bytes:
    """Подобие фото этикетки: светлый прямоугольник с тёмной полосой."""
    image = Image.new("RGB", (width, height), (245, 240, 220))
    for y in range(height // 3, height // 2):
        for x in range(width // 8, width - width // 8):
            image.putpixel((x, y), (60, 60, 60))
    buffer = io.BytesIO()
    image.save(buffer, "JPEG", quality=80, subsampling=0)
    return buffer.getvalue()


def build_svs(
    path: Path,
    size: int = 512,
    tile: int = 256,
    objective: int = 20,
    mpp: float = 0.5,
    with_label: bool = False,
    aperio: bool = True,
) -> Path:
    """aperio=False даёт обычный пирамидальный TIFF без метаданных Aperio:
    OpenSlide открывает его как generic-tiff, увеличения и этикетки у него нет."""
    if not aperio and with_label:
        raise ValueError("этикетка бывает только у синтетического Aperio")
    tiles_per_side = size // tile
    jpegs = [_tile_jpeg(tile, 40 + 30 * i) for i in range(tiles_per_side * tiles_per_side)]
    description = (
        f"Aperio Image Library v11.2.1\r\n{size}x{size} [0,0 {size}x{size}] ({tile}x{tile}) JPEG/RGB Q=70"
        f"|AppMag = {objective}|MPP = {mpp}"
    ).encode("ascii") + b"\x00"
    if not aperio:
        description = b"Synthetic tiled TIFF\x00"
    # В Aperio за основным изображением идёт миниатюра, и только потом этикетка,
    # поэтому оба каталога нужны: иначе OpenSlide принимает этикетку за миниатюру.
    extras = [
        (96, 96, b"Aperio Image Library v11.2.1\r\nthumbnail 96x96\x00"),
        (120, 60, b"Aperio Image Library v11.2.1\r\nlabel 120x60\x00"),
    ]

    body = bytearray()  # всё, что лежит после заголовка и до таблиц тегов
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

    def tiled_entries(level_size: int, level_tile: int, text: bytes) -> list:
        """Каталог с тайлами: основное изображение и уменьшенные уровни пирамиды."""
        per_side = max(1, level_size // level_tile)
        parts = [_tile_jpeg(level_tile, 40 + 30 * i) for i in range(per_side * per_side)]
        starts = [put(part) for part in parts]
        sizes = [len(part) for part in parts]
        return [
            (TAG_WIDTH, LONG, 1, level_size),
            (TAG_LENGTH, LONG, 1, level_size),
            (TAG_BITS, SHORT, 3, put(struct.pack("<3H", 8, 8, 8))),
            (TAG_COMPRESSION, SHORT, 1, 7),
            (TAG_PHOTOMETRIC, SHORT, 1, 6),
            (TAG_DESCRIPTION, ASCII, len(text), put(text)),
            (TAG_SAMPLES, SHORT, 1, 3),
            (TAG_PLANAR, SHORT, 1, 1),
            (TAG_TILE_W, SHORT, 1, level_tile),
            (TAG_TILE_L, SHORT, 1, level_tile),
            (TAG_TILE_OFFSETS, LONG, len(starts), put(struct.pack(f"<{len(starts)}I", *starts))),
            (TAG_TILE_COUNTS, LONG, len(sizes), put(struct.pack(f"<{len(sizes)}I", *sizes))),
            (TAG_YCC_SUBSAMPLING, SHORT, 2, 1 | (1 << 16)),
        ]

    extra_entries = []
    if with_label:
        # Порядок как в настоящем SVS: миниатюра, уровень пирамиды, затем этикетка
        extras.insert(1, None)
        for item in extras:
            if item is None:
                half = max(size // 2, 128)
                extra_entries.append(tiled_entries(
                    half, min(tile, half),
                    f"Aperio Image Library v11.2.1\r\n{size}x{size} -> {half}x{half}".encode("ascii") + b"\x00",
                ))
                continue
            width, height, text = item
            data = _label_jpeg(width, height)
            data_offset = put(data)
            text_offset = put(text)
            bits = put(struct.pack("<3H", 8, 8, 8))
            extra_entries.append([
                (TAG_SUBFILE, LONG, 1, 1 if text.split(b"\n")[1].startswith(b"label") else 0),
                (TAG_WIDTH, LONG, 1, width),
                (TAG_LENGTH, LONG, 1, height),
                (TAG_BITS, SHORT, 3, bits),
                (TAG_COMPRESSION, SHORT, 1, 7),
                (TAG_PHOTOMETRIC, SHORT, 1, 6),
                (TAG_DESCRIPTION, ASCII, len(text), text_offset),
                (TAG_STRIP_OFFSETS, LONG, 1, data_offset),
                (TAG_SAMPLES, SHORT, 1, 3),
                (TAG_ROWS_PER_STRIP, LONG, 1, height),
                (TAG_STRIP_COUNTS, LONG, 1, len(data)),
                (TAG_PLANAR, SHORT, 1, 1),
                (TAG_YCC_SUBSAMPLING, SHORT, 2, 1 | (1 << 16)),
            ])

    main_entries = [
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
        (TAG_TILE_OFFSETS, LONG, n_tiles := len(jpegs), offsets_offset),
        (TAG_TILE_COUNTS, LONG, n_tiles, counts_offset),
        (TAG_YCC_SUBSAMPLING, SHORT, 2, 1 | (1 << 16)),
    ]

    def pack_ifd(entries: list, next_offset: int) -> bytes:
        block = bytearray(struct.pack("<H", len(entries)))
        for tag, kind, count, value in entries:
            block += struct.pack("<HHII", tag, kind, count, value)
        block += struct.pack("<I", next_offset)
        return bytes(block)

    # Каталоги идут подряд, каждый указывает на следующий
    all_entries = [main_entries, *extra_entries]
    offsets, cursor = [], base + len(body)
    for entries in all_entries:
        offsets.append(cursor)
        cursor += 2 + 12 * len(entries) + 4
    main_ifd_offset = offsets[0]
    blocks = b"".join(
        pack_ifd(entries, offsets[index + 1] if index + 1 < len(offsets) else 0)
        for index, entries in enumerate(all_entries)
    )

    header = b"II" + struct.pack("<HI", 42, main_ifd_offset)
    path.write_bytes(header + bytes(body) + blocks)
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
