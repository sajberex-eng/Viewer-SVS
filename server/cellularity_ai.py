"""Оценка клеточности «на глаз» моделью с картинкой — второй способ рядом с алгоритмом
(решение заказчика 2026-09-24, раздел 13.8 ТЗ, шаг 6). Подача изображений — по тексту
заказчика от 2026-09-24: обзор фрагмента плюс два участка ×20 с отметкой их положения на
обзоре. Промт сокращён 2026-09-25 по его же слову: число, разброс, признак неоднородности
по порогу 20 п. п., причины из списка и одна фраза вместо морфологического описания.

Что уходит наружу: обзорное изображение фрагмента в малом увеличении с нарисованным контуром
ткани и два участка внутри контура при разрешении объектива ×20 (около 0,8 мм стороной).
Ни этикеток, ни имён, ни номеров: модель видит срез и возвращает оценку. Ключ доступа
хранится на сервере (data/ai.key, вводится администратором в разделе «Настройки») либо
в переменной ANTHROPIC_API_KEY контейнера.

Два поставщика (решение заказчика 2026-09-25): Gemini API (ключ data/gemini.key или
GEMINI_API_KEY, модель cellularity.gemini_model) и Anthropic. Какой из них основной, выбирает
администратор переключателем в «Настройках» (data/ai.provider); по умолчанию — Gemini. Второй
поставщик — запасной: когда у основного исчерпан лимит (предел запросов, счёт), тот же запрос
уходит к нему. Если ключа основного нет, работает тот, чей ключ задан. Ответ Gemini проверяется
той же схемой Pydantic, в usage запоминается модель каждого запроса.

Как считается, по каждому фрагменту два запроса:
1. обзор с контуром → модель выбирает два участка, которые хочет рассмотреть ближе
   (неоднородные или сомнительные); точки привязываются к ближайшему окну с тканью;
2. обзор с номерами участков и два участка ×20 → средняя клеточность фрагмента, разброс
   по частям, признак неоднородности (порог 20 п. п.), причины из списка, одна фраза.
Стекло — среднее фрагментов с весом площади ткани, разброс — крайние значения фрагментов.
Это оценка, а не измерение: масок и площадей у неё нет.
"""
from __future__ import annotations

import base64
import io
import json
import logging
import math
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Literal

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont
from pydantic import BaseModel, Field

from . import cellularity_slide as cs

log = logging.getLogger(__name__)

OVERVIEW_PX = 1568           # длинная сторона обзора, точек (больше модель всё равно уменьшает)
DETAIL_UM = 800.0            # сторона участка ×20, мкм (поле зрения ×20 около 0,8–1 мм)
DETAIL_PX = 1568             # сторона картинки участка, точек (~0,5 мкм на точку — объектив ×20)
REGIONS = 2                  # участков ×20 на фрагмент — по промту заказчика
GRID_UM = 16.0               # шаг растра ткани при привязке участков
MIN_TISSUE_FRACTION = 0.6    # окно годится под участок, если ткани в нём не меньше этой доли
MAX_TOKENS = 4000            # ответ короткий; запас на размышление модели
EFFORT = "low"               # уровень усилий модели: задача не требует долгого размышления
KEY_FILE = "ai.key"
GEMINI_KEY_FILE = "gemini.key"
PROVIDER_FILE = "ai.provider"
PROVIDERS = ("gemini", "anthropic")
DEFAULT_PROVIDER = "gemini"
GEMINI_MODEL = "gemini-flash-latest"
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
REQUEST_TIMEOUT = 240.0      # на один запрос к любому ИИ, секунд

# Промт заказчика (2026-09-24, сокращён 2026-09-25 по его слову после пробы на случае 1).
# Правила оценки — в системной части, состав картинок и что вернуть — в пользовательской:
# так первая часть одинакова у всех запросов. Менять только по слову заказчика.
SYSTEM_PROMPT = (
    "Вы — опытный гематопатолог, оценивающий клеточность костного мозга на препаратах с окраской H&E "
    "визуально, как у микроскопа.\n"
    "Вам передаются обзор фрагмента трепанобиоптата на малом увеличении (контур оцениваемой ткани зелёный, "
    "артефакты красные) и два участка при ×20, отмеченные на обзоре номерами.\n"
    "Клеточность (%) = площадь кроветворной ткани / суммарная площадь кроветворной и жировой ткани × 100. "
    "Оценивайте площади, а не число ядер и не долю фиолетового цвета.\n"
    "Исключайте кость, кортикальный слой, хрящ, фон, сгустки крови и артефакты. Жировые вакуоли отличайте "
    "от разрывов, сосудов и отёка стромы.\n"
    "Среднюю клеточность и её распределение оценивайте по обзору. Участки ×20 служат только для уточнения "
    "соответствующих зон: их клеточность на весь фрагмент не переносите.\n"
    "Округляйте до 5 процентных пунктов. Фрагмент считайте неоднородным, только если клеточность его частей "
    "различается не меньше чем на 20 процентных пунктов на заметной площади. Лимфоидные узелки и другие очаги "
    "включайте в оценку.\n"
    "Отвечайте кратко: без описания морфологии, без перечисления исключённого, без оговорок о методе и "
    "качестве среза. Положение зон называйте словами (слева, справа, в центре, у кости, по краю), "
    "а не номерами участков."
)
PICK_PROMPT = (
    "Это обзорное изображение фрагмента {n} из {of} трепанобиоптата на малом увеличении: {w} × {h} точек, "
    "около {um:.0f} мкм на точку. Контур оцениваемой ткани обведён зелёной линией, артефакты — красной. "
    "Выберите {k} участка внутри контура, которые нужно рассмотреть при ×20 объектива: неоднородные или "
    "сомнительные, где по обзору нельзя уверенно оценить клеточность. Для каждого укажите центр в пикселях "
    "этого изображения (x слева направо, y сверху вниз) и причину выбора по-русски — не больше шести слов."
)
ESTIMATE_PROMPT = (
    "Изображение 1 — обзор фрагмента {n} из {of} ({um:.0f} мкм на точку); следующие изображения — участки ×20 "
    "по порядку номеров (сторона около {detail:.1f} мм).\n"
    "Верните: cellularity_percent — средняя клеточность фрагмента; regional_min_percent и "
    "regional_max_percent — крайние значения по частям фрагмента; heterogeneous — true или false по порогу "
    "20 п. п.; causes — за счёт чего неоднородность, из списка, только при heterogeneous = true (иначе пустой "
    "список); note — одна фраза до 20 слов: где и какая клеточность отличается (пустая строка при однородной). "
    "Если оценить нельзя — null и причина в note."
)
Cause = Literal["жировые поля", "фиброз стромы", "лимфоидный узелок", "очаг плотных клеток",
                "субкортикальная зона", "отёк или кровоизлияние", "артефакт", "другое"]


class RegionPick(BaseModel):
    x: int = Field(description="центр участка, пикселей от левого края обзорного изображения")
    y: int = Field(description="центр участка, пикселей от верхнего края обзорного изображения")
    reason: str = Field(description="почему этот участок нужно рассмотреть при ×20, не больше шести слов")


class RegionChoice(BaseModel):
    """Ответ модели на первом шаге: какие участки рассмотреть ближе."""
    regions: list[RegionPick]


class FragmentEstimate(BaseModel):
    """Ответ модели на втором шаге — поля по промту заказчика (редакция 2026-09-25)."""
    cellularity_percent: float | None = Field(description="средняя клеточность фрагмента, %")
    regional_min_percent: float | None = Field(description="минимум по частям фрагмента, %")
    regional_max_percent: float | None = Field(description="максимум по частям фрагмента, %")
    heterogeneous: bool | None = Field(description="разница частей не меньше 20 процентных пунктов")
    causes: list[Cause] = Field(description="за счёт чего неоднородность; пусто при однородной")
    note: str = Field(description="одна фраза до 20 слов: где и какая клеточность отличается; пусто при однородной")


class AiUnavailable(Exception):
    """Ключ не задан, модель недоступна или ответила отказом."""


class LimitExhausted(AiUnavailable):
    """Основной ИИ исчерпал лимит (предел запросов или счёт): повод перейти на запасной."""


# ---------- ключи ----------

def key_path(data_dir: Path) -> Path:
    return Path(data_dir) / KEY_FILE


def gemini_key_path(data_dir: Path) -> Path:
    return Path(data_dir) / GEMINI_KEY_FILE


def _read_key(path: Path, env_name: str) -> str | None:
    env = os.environ.get(env_name, "").strip()
    if env:
        return env
    try:
        value = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return value or None


def _write_key(path: Path, value: str | None) -> None:
    """Сохранить ключ (пустой — удалить). Файл только для владельца; в журнал и ответы не попадает."""
    if not value or not value.strip():
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value.strip(), encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def api_key(data_dir: Path) -> str | None:
    return _read_key(key_path(data_dir), "ANTHROPIC_API_KEY")


def set_api_key(data_dir: Path, value: str | None) -> None:
    _write_key(key_path(data_dir), value)


def gemini_key(data_dir: Path) -> str | None:
    return _read_key(gemini_key_path(data_dir), "GEMINI_API_KEY")


def set_gemini_key(data_dir: Path, value: str | None) -> None:
    _write_key(gemini_key_path(data_dir), value)


def provider(data_dir: Path, default: str = DEFAULT_PROVIDER) -> str:
    """Основной поставщик: выбор администратора (data/ai.provider), иначе настройка по умолчанию."""
    try:
        value = (Path(data_dir) / PROVIDER_FILE).read_text(encoding="utf-8").strip().lower()
    except OSError:
        value = ""
    if value in PROVIDERS:
        return value
    return default if default in PROVIDERS else DEFAULT_PROVIDER


def set_provider(data_dir: Path, value: str) -> None:
    if value not in PROVIDERS:
        raise ValueError("неизвестный поставщик ИИ")
    path = Path(data_dir) / PROVIDER_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")


# ---------- ткань фрагмента в грубом растре ----------

def tissue_raster(fragment: cs.Fragment, base_mpp: float) -> tuple[np.ndarray, float]:
    """Маска ткани фрагмента в грубом растре (GRID_UM на точку) и её шаг в точках уровня 0."""
    step = GRID_UM / base_mpp
    w, h = cs.analysis_size(fragment.bbox, step)
    tissue, art = cs.fragment_masks(fragment, step, (h, w))
    return tissue & ~art, step


def _candidates(mask: np.ndarray, side: int, stride: int) -> list[tuple[float, int, int]]:
    """Окна side × side растра с шагом stride, где ткани не меньше MIN_TISSUE_FRACTION: (доля, x, y)."""
    h, w = mask.shape
    if h == 0 or w == 0 or not mask.any():
        return []
    integral = np.pad(mask.astype(np.int64).cumsum(0).cumsum(1), ((1, 0), (1, 0)))
    found = []
    for y in range(0, max(h - side, 0) + 1, stride):
        for x in range(0, max(w - side, 0) + 1, stride):
            y1, x1 = min(y + side, h), min(x + side, w)
            area = (y1 - y) * (x1 - x)
            filled = integral[y1, x1] - integral[y, x1] - integral[y1, x] + integral[y, x]
            fraction = filled / area if area else 0.0
            if fraction >= MIN_TISSUE_FRACTION:
                found.append((fraction, x, y))
    if not found:
        # фрагмент мельче окна: одно окно по центру, если ткани хоть сколько-то
        fraction = float(mask.mean())
        if fraction > 0:
            found.append((fraction, max((w - side) // 2, 0), max((h - side) // 2, 0)))
    return found


def _to_region(fragment: cs.Fragment, base_mpp: float, step: float, cand, reason: str = "") -> dict:
    x0, y0 = fragment.bbox[0], fragment.bbox[1]
    return {"x": int(round(x0 + cand[1] * step)), "y": int(round(y0 + cand[2] * step)),
            "size": int(round(DETAIL_UM / base_mpp)), "tissue_fraction": round(float(cand[0]), 2),
            "reason": reason}


def sample_fields(fragment: cs.Fragment, base_mpp: float, count: int) -> list[dict]:
    """Участки фрагмента без модели: квадраты стороной DETAIL_UM, разнесённые по ткани.

    Запасной выбор, когда модель не указала годных точек: первое окно — с наибольшей долей
    ткани, каждое следующее — самое далёкое от уже выбранных.
    """
    mask, step = tissue_raster(fragment, base_mpp)
    side = max(1, int(round(DETAIL_UM / GRID_UM)))
    candidates = _candidates(mask, side, max(1, side // 2))
    if not candidates:
        return []
    chosen = [max(candidates, key=lambda c: c[0])]
    while len(chosen) < count and len(chosen) < len(candidates):
        def distance(c):
            return min(math.hypot(c[1] - k[1], c[2] - k[2]) for k in chosen)
        rest = [c for c in candidates if c not in chosen]
        best = max(rest, key=lambda c: (distance(c), c[0]))
        if distance(best) < side * 0.5:
            break  # дальше окна только перекрываются
        chosen.append(best)
    return [_to_region(fragment, base_mpp, step, c) for c in chosen]


def snap_regions(fragment: cs.Fragment, base_mpp: float, picks: list[dict], overview_ds: float,
                 count: int = REGIONS) -> list[dict]:
    """Точки модели (пиксели обзора) → участки ×20 уровня 0, привязанные к ближайшему окну с тканью.

    Одно и то же окно дважды не берётся; недостающие участки добираются запасным выбором.
    """
    mask, step = tissue_raster(fragment, base_mpp)
    side = max(1, int(round(DETAIL_UM / GRID_UM)))
    candidates = _candidates(mask, side, max(1, side // 4))
    chosen: list[tuple] = []
    regions: list[dict] = []
    for pick in picks:
        if len(regions) >= count or not candidates:
            break
        # центр точки модели в растре ткани
        gx = float(pick["x"]) * overview_ds / step
        gy = float(pick["y"]) * overview_ds / step
        rest = [c for c in candidates if c not in chosen]
        if not rest:
            break
        best = min(rest, key=lambda c: math.hypot(c[1] + side / 2 - gx, c[2] + side / 2 - gy))
        chosen.append(best)
        regions.append(_to_region(fragment, base_mpp, step, best, str(pick.get("reason") or "")[:200]))
    if len(regions) < count:
        for region in sample_fields(fragment, base_mpp, count * 2):
            if len(regions) >= count:
                break
            if all(abs(region["x"] - r["x"]) + abs(region["y"] - r["y"]) > region["size"] * 0.5 for r in regions):
                region["reason"] = region["reason"] or "выбран автоматически"
                regions.append(region)
    for n, region in enumerate(regions, 1):
        region["n"] = n
    return regions


def tissue_area_mm2(fragment: cs.Fragment, base_mpp: float) -> float:
    mask, step = tissue_raster(fragment, base_mpp)
    return float(mask.sum()) * (step * base_mpp) ** 2 / 1e6


# ---------- картинки ----------

def _read_rgb(slide, location, level: int, size) -> Image.Image:
    region = slide.read_region(location, level, size)
    image = Image.new("RGB", region.size, (255, 255, 255))
    image.paste(region, mask=region.getchannel("A"))
    return image


def _level_for(slide, factor: float) -> int:
    """Ближайший уровень пирамиды не грубее нужного уменьшения."""
    return max(i for i, d in enumerate(slide.level_downsamples) if d <= factor * 1.02)


def read_overview(slide, fragment: cs.Fragment) -> tuple[Image.Image, float]:
    """Обзор фрагмента с длинной стороной не больше OVERVIEW_PX и его масштаб (точек уровня 0 на точку)."""
    x0, y0, x1, y1 = fragment.bbox
    w, h = max(1, x1 - x0), max(1, y1 - y0)
    need = max(1.0, max(w, h) / OVERVIEW_PX)
    level = _level_for(slide, need)
    lds = slide.level_downsamples[level]
    image = _read_rgb(slide, (x0, y0), level, (max(1, int(round(w / lds))), max(1, int(round(h / lds)))))
    scale = max(image.size) / OVERVIEW_PX
    if scale > 1:
        image = image.resize((max(1, int(round(image.width / scale))), max(1, int(round(image.height / scale)))),
                             Image.Resampling.LANCZOS)
    return image, w / image.width


def draw_contours(image: Image.Image, fragment: cs.Fragment, ds: float) -> Image.Image:
    """Контур ткани зелёным, артефакты красным — модель оценивает внутри контура."""
    w, h = image.size
    tissue, art = cs.fragment_masks(fragment, ds, (h, w))
    draw = ImageDraw.Draw(image)
    width = max(2, w // 400)
    for mask, color in ((tissue, (0, 200, 0)), (art, (230, 0, 0))):
        if not mask.any():
            continue
        contours, _ = cv2.findContours(mask.astype(np.uint8), cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
        for contour in contours:
            points = [tuple(int(v) for v in p[0]) for p in contour]
            if len(points) >= 3:
                draw.line(points + points[:1], fill=color, width=width)
    return image


def mark_regions(image: Image.Image, regions: list[dict], fragment: cs.Fragment, ds: float) -> Image.Image:
    """Прямоугольники участков с номерами на обзоре."""
    image = image.copy()
    draw = ImageDraw.Draw(image)
    x0, y0 = fragment.bbox[0], fragment.bbox[1]
    width = max(2, image.width // 300)
    font_size = max(18, image.width // 40)
    try:
        font = ImageFont.load_default(size=font_size)
    except TypeError:  # старый Pillow без размера у шрифта по умолчанию
        font = ImageFont.load_default()
    for region in regions:
        left = (region["x"] - x0) / ds
        top = (region["y"] - y0) / ds
        side = region["size"] / ds
        draw.rectangle([left, top, left + side, top + side], outline=(255, 140, 0), width=width)
        label = str(region["n"])
        box = draw.textbbox((0, 0), label, font=font)
        pad = max(3, font_size // 5)
        bw, bh = box[2] - box[0] + 2 * pad, box[3] - box[1] + 2 * pad
        lx, ly = max(0, left - bw / 2), max(0, top - bh / 2)
        draw.rectangle([lx, ly, lx + bw, ly + bh], fill=(255, 140, 0))
        draw.text((lx + pad - box[0], ly + pad - box[1]), label, fill=(0, 0, 0), font=font)
    return image


def read_detail(slide, region: dict) -> Image.Image:
    """Участок ×20: DETAIL_PX × DETAIL_PX, читается с ближайшего уровня не грубее нужного."""
    factor = max(region["size"] / DETAIL_PX, 1e-6)
    level = _level_for(slide, factor) if factor >= 1 else 0
    lds = slide.level_downsamples[level]
    side = max(1, int(round(region["size"] / lds)))
    image = _read_rgb(slide, (region["x"], region["y"]), level, (side, side))
    if image.size != (DETAIL_PX, DETAIL_PX) and side > DETAIL_PX:
        image = image.resize((DETAIL_PX, DETAIL_PX), Image.Resampling.LANCZOS)
    return image


def jpeg(image: Image.Image, quality: int = 85) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, "JPEG", quality=quality)
    return buffer.getvalue()


# ---------- модели ----------

class AiClients:
    """Оба поставщика; любого из них может не быть. primary — основной, второй — запасной."""

    def __init__(self, anthropic_key: str | None, gemini_key: str | None = None, gemini_model: str = GEMINI_MODEL,
                 primary: str = DEFAULT_PROVIDER):
        self.primary = primary if primary in PROVIDERS else DEFAULT_PROVIDER
        self.anthropic = None
        if anthropic_key:
            import anthropic

            self.anthropic = anthropic.Anthropic(api_key=anthropic_key, max_retries=3, timeout=REQUEST_TIMEOUT)
        self.gemini_key = gemini_key or None
        self.gemini_model = gemini_model


def make_client(key: str | None, gemini_key: str | None = None, gemini_model: str = GEMINI_MODEL,
                primary: str = DEFAULT_PROVIDER) -> AiClients:
    return AiClients(key, gemini_key, gemini_model, primary)


def _ask_anthropic(client, model: str, images: list[bytes], text: str, schema) -> dict | None:
    import anthropic

    content = [{"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                             "data": base64.standard_b64encode(data).decode("ascii")}}
               for data in images]
    content.append({"type": "text", "text": text})
    try:
        response = client.messages.parse(
            model=model,
            max_tokens=MAX_TOKENS,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": content}],
            output_format=schema,
            output_config={"effort": EFFORT},
        )
    except anthropic.AuthenticationError as exc:
        raise AiUnavailable("ключ доступа к ИИ не принят: проверьте его в разделе «Настройки»") from exc
    except anthropic.RateLimitError as exc:
        raise LimitExhausted("ИИ временно недоступен: превышен предел запросов, попробуйте позже") from exc
    except anthropic.APIConnectionError as exc:
        raise AiUnavailable("нет связи с ИИ: сервер не достучался до api.anthropic.com") from exc
    except anthropic.APIStatusError as exc:
        # исчерпанный счёт приходит как 400 с текстом про credit balance, перегрузка — 529
        if exc.status_code in (402, 529) or "credit" in str(exc).lower():
            raise LimitExhausted(f"ИИ недоступен: {exc.status_code}, исчерпан лимит или счёт") from exc
        raise AiUnavailable(f"ИИ ответил ошибкой {exc.status_code}") from exc
    if response.stop_reason == "refusal" or response.parsed_output is None:
        return None
    out = response.parsed_output.model_dump()
    out["_usage"] = {"model": model, "input_tokens": response.usage.input_tokens,
                     "output_tokens": response.usage.output_tokens}
    return out


def gemini_schema(model_class) -> dict:
    """Схема ответа для Gemini из модели Pydantic: подмножество OpenAPI — без $ref и anyOf,
    «или null» превращается в nullable, типы прописными."""
    raw = model_class.model_json_schema()
    defs = raw.get("$defs", {})

    def convert(node: dict) -> dict:
        if "$ref" in node:
            node = defs[node["$ref"].rsplit("/", 1)[-1]]
        out: dict = {}
        nullable = False
        if "anyOf" in node:
            variants = [v for v in node["anyOf"] if v.get("type") != "null"]
            nullable = len(variants) < len(node["anyOf"])
            node = {**node, **(variants[0] if variants else {})}
            node.pop("anyOf", None)
        if "type" in node:
            out["type"] = str(node["type"]).upper()
        if "enum" in node:
            out["enum"] = list(node["enum"])
        if "description" in node:
            out["description"] = node["description"]
        if node.get("type") == "object":
            out["properties"] = {name: convert(prop) for name, prop in node.get("properties", {}).items()}
            if node.get("required"):
                out["required"] = list(node["required"])
        if node.get("type") == "array" and "items" in node:
            out["items"] = convert(node["items"])
        if nullable:
            out["nullable"] = True
        return out

    return convert(raw)


def gemini_request(url: str, key: str, body: dict) -> dict:
    """Один HTTP-запрос к Gemini API; подменяется в тестах."""
    data = json.dumps(body).encode("utf-8")
    request = urllib.request.Request(url, data=data, method="POST", headers={
        "Content-Type": "application/json", "x-goog-api-key": key})
    with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
        return json.loads(response.read().decode("utf-8"))


def _ask_gemini(key: str, model: str, images: list[bytes], text: str, schema) -> dict | None:
    parts = [{"inlineData": {"mimeType": "image/jpeg", "data": base64.standard_b64encode(d).decode("ascii")}}
             for d in images]
    parts.append({"text": text})
    body = {
        "systemInstruction": {"parts": [{"text": SYSTEM_PROMPT}]},
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": {"responseMimeType": "application/json", "responseSchema": gemini_schema(schema),
                             "maxOutputTokens": MAX_TOKENS},
    }
    try:
        reply = gemini_request(GEMINI_URL.format(model=model), key, body)
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise AiUnavailable("ключ Gemini не принят: проверьте его в разделе «Настройки»") from exc
        if exc.code == 429:
            raise LimitExhausted("Gemini: превышен предел запросов или квота") from exc
        raise AiUnavailable(f"Gemini ответил ошибкой {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise AiUnavailable("нет связи с Gemini: сервер не достучался до generativelanguage.googleapis.com") from exc
    candidates = reply.get("candidates") or []
    if not candidates or (reply.get("promptFeedback") or {}).get("blockReason"):
        return None
    content = candidates[0].get("content") or {}
    raw_text = "".join(p.get("text", "") for p in content.get("parts") or [])
    try:
        parsed = schema.model_validate(json.loads(raw_text))
    except Exception as exc:  # ответ не по схеме — как отказ, без падения оценки
        log.warning("Gemini: ответ не разобран (%s)", exc)
        return None
    out = parsed.model_dump()
    meta = reply.get("usageMetadata") or {}
    out["_usage"] = {"model": model, "input_tokens": int(meta.get("promptTokenCount") or 0),
                     "output_tokens": int(meta.get("candidatesTokenCount") or 0) + int(meta.get("thoughtsTokenCount") or 0)}
    return out


def ask_model(client: AiClients, model: str, images: list[bytes], text: str, schema) -> dict | None:
    """Один запрос: картинки и текст → ответ по строгой схеме (structured outputs).

    Сначала основной поставщик (client.primary); при исчерпании его лимита — второй, если задан
    его ключ. model — модель Anthropic. None — модель отказалась отвечать или ответ не
    разобрался. В ответе ключ "_usage" с моделью и токенами запроса: из них складывается
    стоимость оценки.
    """
    order = ["gemini", "anthropic"] if client.primary == "gemini" else ["anthropic", "gemini"]
    have = {"gemini": bool(client.gemini_key), "anthropic": client.anthropic is not None}
    chain = [name for name in order if have[name]]
    if not chain:
        raise AiUnavailable("ключ доступа к ИИ не задан")
    last: LimitExhausted | None = None
    for name in chain:
        try:
            if name == "gemini":
                return _ask_gemini(client.gemini_key, client.gemini_model, images, text, schema)
            return _ask_anthropic(client.anthropic, model, images, text, schema)
        except LimitExhausted as exc:
            last = exc
            if name != chain[-1]:
                log.warning("ИИ %s: %s — запрос уходит к запасному", name, exc)
    raise last


# ---------- расчёт по стеклу ----------

def _pct(value) -> float | None:
    if value is None:
        return None
    try:
        return round(min(100.0, max(0.0, float(value))), 1)
    except (TypeError, ValueError):
        return None


def _label(default_model: str, models: dict) -> str:
    """Подпись расчёта: модель, ответившая больше всех, и — если запросы делились — остальные."""
    if not models:
        return f"оценка по обзору и участкам ×20, модель {default_model}"
    ranked = sorted(models.items(), key=lambda item: (-item[1], item[0]))
    text = f"оценка по обзору и участкам ×20, модель {ranked[0][0]}"
    if len(ranked) > 1:
        text += "; часть запросов по исчерпании лимита: " + ", ".join(f"{name} {n}" for name, n in ranked[1:])
    return text


def estimate(slide, base_mpp: float, fragments: list[cs.Fragment], client, model: str,
             progress=None, should_stop=None, ask=ask_model, keep_images=None) -> dict:
    """Оценка по стеклу: на каждый фрагмент обзор → выбор участков → оценка с описанием.

    progress(fragment, of, stage) — stage 'pick' или 'estimate'; should_stop() — отмена между
    запросами (InterruptedError). ask подменяется в тестах, чтобы не ходить в сеть;
    keep_images(fragment, name, image) — для стенда, чтобы посмотреть, что уходит модели.
    """
    total = len(fragments)
    results = []
    usage = {"requests": 0, "input_tokens": 0, "output_tokens": 0, "models": {}}

    def account(answer):
        if answer is None:
            return
        used = answer.pop("_usage", None)
        if used:
            usage["requests"] += 1
            name = str(used.get("model") or model)
            usage["models"][name] = usage["models"].get(name, 0) + 1
            usage["input_tokens"] += int(used.get("input_tokens") or 0)
            usage["output_tokens"] += int(used.get("output_tokens") or 0)

    for index, fragment in enumerate(fragments, 1):
        if should_stop and should_stop():
            raise InterruptedError
        if progress:
            progress(index, total, "pick")
        overview, ds = read_overview(slide, fragment)
        overview = draw_contours(overview, fragment, ds)
        um = ds * base_mpp
        choice = ask(client, model, [jpeg(overview)],
                     PICK_PROMPT.format(n=index, of=total, w=overview.width, h=overview.height, um=um, k=REGIONS),
                     RegionChoice)
        account(choice)
        picks = list((choice or {}).get("regions") or [])[:REGIONS]
        regions = snap_regions(fragment, base_mpp, picks, ds)
        if not regions:
            results.append({"fragment": index, "tissue_mm2": round(tissue_area_mm2(fragment, base_mpp), 4),
                            "cellularity_eq1_pct": None, "regional_min_pct": None, "regional_max_pct": None,
                            "heterogeneous": None, "causes": [], "description": "", "regions": [],
                            "fields_used": 0, "warnings": ["внутри контура не нашлось участков с тканью"]})
            continue
        if should_stop and should_stop():
            raise InterruptedError
        if progress:
            progress(index, total, "estimate")
        marked = mark_regions(overview, regions, fragment, ds)
        details = [read_detail(slide, region) for region in regions]
        if keep_images:
            keep_images(index, "overview", marked)
            for region, detail in zip(regions, details):
                keep_images(index, f"region{region['n']}", detail)
        answer = ask(client, model, [jpeg(marked), *(jpeg(d) for d in details)],
                     ESTIMATE_PROMPT.format(n=index, of=total, um=um, detail=DETAIL_UM / 1000),
                     FragmentEstimate)
        account(answer)
        item = {"fragment": index, "tissue_mm2": round(tissue_area_mm2(fragment, base_mpp), 4),
                "regions": [{k: v for k, v in r.items() if k != "tissue_fraction"} for r in regions],
                "fields_used": len(regions), "warnings": []}
        if answer is None:
            item.update({"cellularity_eq1_pct": None, "regional_min_pct": None, "regional_max_pct": None,
                         "heterogeneous": None, "causes": [], "description": "",
                         "warnings": ["модель не дала оценку по этому фрагменту"]})
        else:
            pct = _pct(answer.get("cellularity_percent"))
            lo, hi = _pct(answer.get("regional_min_percent")), _pct(answer.get("regional_max_percent"))
            if lo is not None and hi is not None and lo > hi:
                lo, hi = hi, lo
            hetero = answer.get("heterogeneous")
            # причины имеют смысл только при неоднородности: модель иногда заполняет их и без неё
            causes = [str(c) for c in (answer.get("causes") or [])] if hetero else []
            item.update({"cellularity_eq1_pct": pct, "regional_min_pct": lo, "regional_max_pct": hi,
                         "heterogeneous": hetero, "causes": causes,
                         "description": " ".join(str(answer.get("note") or "").split())[:300]})
            if pct is None:
                item["warnings"].append("модель не смогла оценить клеточность фрагмента")
        results.append(item)
    weighted = [(r["cellularity_eq1_pct"], r["tissue_mm2"]) for r in results if r["cellularity_eq1_pct"] is not None]
    total_pct = (round(sum(p * a for p, a in weighted) / sum(a for _, a in weighted), 1)
                 if weighted and sum(a for _, a in weighted) > 0 else None)
    lows = [r["regional_min_pct"] if r["regional_min_pct"] is not None else r["cellularity_eq1_pct"]
            for r in results if r["cellularity_eq1_pct"] is not None]
    highs = [r["regional_max_pct"] if r["regional_max_pct"] is not None else r["cellularity_eq1_pct"]
             for r in results if r["cellularity_eq1_pct"] is not None]
    means = [p for p, _ in weighted]
    heterogeneous = (any(r["heterogeneous"] for r in results) or
                     (len(means) > 1 and max(means) - min(means) >= 10)) if means else None
    return {
        "method": "ai",
        "algorithm": _label(model, usage["models"]),
        "usage": usage,
        "pixel_um": round(DETAIL_UM / DETAIL_PX, 3),
        "fragments": results,
        "total": {
            "cellularity_eq1_pct": total_pct,
            "regional_min_pct": min(lows) if lows else None,
            "regional_max_pct": max(highs) if highs else None,
            "heterogeneous": heterogeneous,
            "tissue_mm2": round(sum(r["tissue_mm2"] for r in results), 4),
            "fields_used": sum(r["fields_used"] for r in results),
            "warnings": [] if total_pct is not None else ["модель не смогла оценить ни один фрагмент"],
        },
    }
