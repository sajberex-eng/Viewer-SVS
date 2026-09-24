"""Оценка клеточности «на глаз» моделью с картинкой — второй способ рядом с алгоритмом
(решение заказчика 2026-09-24, раздел 13.8 ТЗ, шаг 6).

Что уходит наружу: только квадратные поля зрения препарата (~1 мм при ~1,3 мкм на точку),
вырезанные внутри контуров ткани. Ни этикеток, ни имён, ни номеров: модель видит срез и
возвращает процент. Ключ доступа хранится на сервере (data/ai.key, вводится администратором
в разделе «Настройки») либо в переменной ANTHROPIC_API_KEY контейнера.

Как считается: в каждом фрагменте выбирается несколько полей, равномерно разнесённых по
ткани (кандидаты — клетки сетки с долей ткани не меньше MIN_TISSUE_FRACTION); каждое поле
модель оценивает отдельно — доля кроветворной ткани среди кроветворной ткани и жира, как
формула 1 у алгоритма; фрагмент — среднее по своим полям, стекло — среднее по фрагментам с
весом площади ткани. Поля, где модель не нашла костный мозг (кость, кровь, артефакт), в
среднее не входят. Это оценка, а не измерение: масок и площадей у неё нет.
"""
from __future__ import annotations

import base64
import io
import math
import os
from pathlib import Path

import numpy as np
from PIL import Image
from pydantic import BaseModel, Field

from . import cellularity_slide as cs

FIELD_UM = 1000.0            # сторона поля зрения, мкм (объектив 10×: поле около 1 мм)
FIELD_PX = 768               # сторона картинки для модели, точек (~1,3 мкм на точку)
GRID_UM = 16.0               # шаг растра ткани при выборе полей
MIN_TISSUE_FRACTION = 0.6    # поле берётся, если ткани в нём не меньше этой доли
MAX_TOKENS = 2000
KEY_FILE = "ai.key"

SYSTEM_PROMPT = (
    "You are an experienced hematopathologist estimating bone marrow cellularity on H&E-stained "
    "trephine biopsy fields, as done visually at the microscope. Cellularity is the percentage of the "
    "marrow space (the area between bone trabeculae, excluding bone, cortex, cartilage, blood clots and "
    "artifacts) that is occupied by hematopoietic cells rather than fat cells. Look at the whole field, "
    "not at individual cells. Answer only with the requested JSON."
)
USER_PROMPT = (
    "Estimate the bone marrow cellularity of this field (about 1 mm across, hematoxylin and eosin). "
    "Give cellularity_pct as an integer from 0 to 100. If the field is mostly bone, cortex, blood, "
    "cartilage or artifact and the marrow space cannot be assessed, set applicable to false."
)


class FieldEstimate(BaseModel):
    """Ответ модели по одному полю зрения (строгая схема через structured outputs)."""
    applicable: bool = Field(description="false if the marrow space cannot be assessed in this field")
    cellularity_pct: int = Field(ge=0, le=100, description="hematopoietic cells as percent of marrow space")
    confidence: str = Field(description="low, medium or high")
    note: str = Field(description="one short sentence on what determined the estimate")


class AiUnavailable(Exception):
    """Ключ не задан, модель недоступна или ответила отказом."""


# ---------- ключ ----------

def key_path(data_dir: Path) -> Path:
    return Path(data_dir) / KEY_FILE


def api_key(data_dir: Path) -> str | None:
    env = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if env:
        return env
    path = key_path(data_dir)
    try:
        value = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return value or None


def set_api_key(data_dir: Path, value: str | None) -> None:
    """Сохранить ключ (пустой — удалить). Файл только для владельца; в журнал и ответы не попадает."""
    path = key_path(data_dir)
    if not value or not value.strip():
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value.strip(), encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


# ---------- выбор полей зрения ----------

def tissue_raster(fragment: cs.Fragment, base_mpp: float) -> tuple[np.ndarray, float]:
    """Маска ткани фрагмента в грубом растре (GRID_UM на точку) и её шаг в точках уровня 0."""
    step = GRID_UM / base_mpp
    w, h = cs.analysis_size(fragment.bbox, step)
    tissue, art = cs.fragment_masks(fragment, step, (h, w))
    return tissue & ~art, step


def sample_fields(fragment: cs.Fragment, base_mpp: float, count: int) -> list[dict]:
    """Поля зрения фрагмента: квадраты стороной FIELD_UM в точках уровня 0, разнесённые по ткани.

    Кандидаты — окна сетки с шагом в половину поля, где ткани не меньше MIN_TISSUE_FRACTION;
    берутся жадно: первое — с наибольшей долей ткани, каждое следующее — самое далёкое от
    уже выбранных (среди тех, где ткани достаточно), чтобы поля не легли рядом.
    """
    mask, step = tissue_raster(fragment, base_mpp)
    side = max(1, int(round(FIELD_UM / GRID_UM)))            # сторона поля в точках растра
    stride = max(1, side // 2)
    h, w = mask.shape
    if h == 0 or w == 0 or not mask.any():
        return []
    integral = np.pad(mask.astype(np.int64).cumsum(0).cumsum(1), ((1, 0), (1, 0)))
    candidates = []
    for y in range(0, max(h - side, 0) + 1, stride):
        for x in range(0, max(w - side, 0) + 1, stride):
            y1, x1 = min(y + side, h), min(x + side, w)
            area = (y1 - y) * (x1 - x)
            filled = integral[y1, x1] - integral[y, x1] - integral[y1, x] + integral[y, x]
            fraction = filled / area if area else 0.0
            if fraction >= MIN_TISSUE_FRACTION:
                candidates.append((fraction, x, y))
    if not candidates and h * w:
        # фрагмент мельче поля: одно поле по центру, если ткани хоть сколько-то
        fraction = float(mask.mean())
        if fraction > 0:
            candidates.append((fraction, max((w - side) // 2, 0), max((h - side) // 2, 0)))
    if not candidates:
        return []
    chosen = [max(candidates, key=lambda c: c[0])]
    while len(chosen) < count and len(chosen) < len(candidates):
        def distance(c):
            return min(math.hypot(c[1] - k[1], c[2] - k[2]) for k in chosen)
        rest = [c for c in candidates if c not in chosen]
        best = max(rest, key=lambda c: (distance(c), c[0]))
        if distance(best) < side * 0.5:
            break  # дальше поля только перекрываются
        chosen.append(best)
    x0, y0 = fragment.bbox[0], fragment.bbox[1]
    size = int(round(FIELD_UM / base_mpp))
    return [{"x": int(round(x0 + c[1] * step)), "y": int(round(y0 + c[2] * step)), "size": size,
             "tissue_fraction": round(float(c[0]), 2)} for c in chosen]


def tissue_area_mm2(fragment: cs.Fragment, base_mpp: float) -> float:
    mask, step = tissue_raster(fragment, base_mpp)
    return float(mask.sum()) * (step * base_mpp) ** 2 / 1e6


# ---------- картинка поля ----------

def read_field(slide, field: dict) -> bytes:
    """JPEG поля зрения FIELD_PX × FIELD_PX: читается с ближайшего уровня пирамиды не грубее нужного."""
    factor = field["size"] / FIELD_PX
    level = max(i for i, d in enumerate(slide.level_downsamples) if d <= factor * 1.02)
    lds = slide.level_downsamples[level]
    side = max(1, int(round(field["size"] / lds)))
    region = slide.read_region((field["x"], field["y"]), level, (side, side))
    image = Image.new("RGB", region.size, (255, 255, 255))
    image.paste(region, mask=region.getchannel("A"))
    image = image.resize((FIELD_PX, FIELD_PX), Image.Resampling.LANCZOS)
    buffer = io.BytesIO()
    image.save(buffer, "JPEG", quality=85)
    return buffer.getvalue()


# ---------- модель ----------

def make_client(key: str):
    import anthropic

    return anthropic.Anthropic(api_key=key, max_retries=3, timeout=120.0)


def ask_model(client, model: str, jpeg: bytes) -> dict:
    """Оценка одного поля моделью с картинкой. Ответ проверяется по схеме FieldEstimate."""
    import anthropic

    try:
        response = client.messages.parse(
            model=model,
            max_tokens=MAX_TOKENS,
            system=SYSTEM_PROMPT,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                                 "data": base64.standard_b64encode(jpeg).decode("ascii")}},
                    {"type": "text", "text": USER_PROMPT},
                ],
            }],
            output_format=FieldEstimate,
        )
    except anthropic.AuthenticationError as exc:
        raise AiUnavailable("ключ доступа к ИИ не принят: проверьте его в разделе «Настройки»") from exc
    except anthropic.RateLimitError as exc:
        raise AiUnavailable("ИИ временно недоступен: превышен предел запросов, попробуйте позже") from exc
    except anthropic.APIConnectionError as exc:
        raise AiUnavailable("нет связи с ИИ: сервер не достучался до api.anthropic.com") from exc
    except anthropic.APIStatusError as exc:
        raise AiUnavailable(f"ИИ ответил ошибкой {exc.status_code}") from exc
    if response.stop_reason == "refusal" or response.parsed_output is None:
        return {"applicable": False, "cellularity_pct": 0, "confidence": "low", "note": "модель не дала оценку"}
    parsed = response.parsed_output
    return {"applicable": parsed.applicable, "cellularity_pct": int(parsed.cellularity_pct),
            "confidence": parsed.confidence, "note": parsed.note}


# ---------- расчёт по стеклу ----------

def estimate(slide, base_mpp: float, fragments: list[cs.Fragment], client, model: str,
             fields_per_fragment: int, progress=None, should_stop=None, ask=ask_model) -> dict:
    """Оценка по стеклу: поля каждого фрагмента → модель → среднее по фрагменту и по стеклу.

    progress(done, total) — по полям; should_stop() — отмена между полями (JobCancelled
    бросает вызывающий код). ask подменяется в тестах, чтобы не ходить в сеть.
    """
    plan = [(index, fragment, sample_fields(fragment, base_mpp, fields_per_fragment))
            for index, fragment in enumerate(fragments, 1)]
    total_fields = sum(len(fields) for _, _, fields in plan)
    if total_fields == 0:
        raise AiUnavailable("внутри контуров не нашлось полей зрения с тканью")
    done = 0
    results = []
    for index, fragment, fields in plan:
        estimates = []
        for field in fields:
            if should_stop and should_stop():
                raise InterruptedError
            answer = ask(client, model, read_field(slide, field))
            estimates.append({**field, **answer})
            done += 1
            if progress:
                progress(done, total_fields)
        usable = [e["cellularity_pct"] for e in estimates if e["applicable"]]
        results.append({
            "fragment": index,
            "tissue_mm2": round(tissue_area_mm2(fragment, base_mpp), 4),
            "fields": estimates,
            "fields_used": len(usable),
            "cellularity_eq1_pct": round(sum(usable) / len(usable), 1) if usable else None,
            "warnings": [] if usable else ["ни в одном поле фрагмента модель не нашла костный мозг"],
        })
    weighted = [(r["cellularity_eq1_pct"], r["tissue_mm2"]) for r in results if r["cellularity_eq1_pct"] is not None]
    total_pct = (round(sum(p * a for p, a in weighted) / sum(a for _, a in weighted), 1)
                 if weighted and sum(a for _, a in weighted) > 0 else None)
    return {
        "method": "ai",
        "algorithm": f"оценка по полям зрения, модель {model}",
        "pixel_um": round(FIELD_UM / FIELD_PX, 3),
        "fragments": results,
        "total": {
            "cellularity_eq1_pct": total_pct,
            "tissue_mm2": round(sum(r["tissue_mm2"] for r in results), 4),
            "fields_used": sum(r["fields_used"] for r in results),
            "warnings": [] if total_pct is not None else ["модель не смогла оценить ни одно поле"],
        },
    }
