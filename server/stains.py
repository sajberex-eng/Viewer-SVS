"""Окраска скана (ОК-1…ОК-3, раздел 13.8 ТЗ).

Окраска хранится кодом из списка, у ИГХ отдельно — маркер свободным текстом.
Подписи для интерфейса живут в словаре `i18n.js`; здесь — только короткие
подписи для названия скана по умолчанию («P004512 · S03 · H&E»), которое
собирает сервер.
"""
from __future__ import annotations

import re

HE, AE, GOMORI, CONGO, IHC = "HE", "AE", "GOMORI", "CONGO", "IHC"

# Порядок — как в списке выбора
CODES = (HE, AE, GOMORI, CONGO, IHC)

SHORT = {HE: "H&E", AE: "АЭ", GOMORI: "Гомори", CONGO: "Конго", IHC: "ИГХ"}

MAX_MARKER_LENGTH = 60

# Как окраска бывает записана в имени файла и в базе до версии 6
_ALIASES = {"HE": HE, "H&E": HE, "H-E": HE, "HANDE": HE}


class StainError(ValueError):
    """Недопустимое значение окраски: показывается пользователю как есть."""


def from_text(text: str | None) -> tuple[str | None, str | None]:
    """Свободный текст (имя файла, база до версии 6) → (код, маркер ИГХ).

    Известное обозначение H&E становится кодом, любой другой непустой текст —
    маркером ИГХ: до версии 6 так записывались только маркеры (Ki-67, CD3…).
    """
    text = " ".join((text or "").split())
    if not text:
        return None, None
    code = _ALIASES.get(text.upper())
    if code:
        return code, None
    return IHC, text[:MAX_MARKER_LENGTH]


# Обозначения в имени файла (<код>_<стекло>_<окраска>): только однозначные.
# Незнакомое слово окраской не считается — PAS или Перлс не ИГХ, а угадывать
# нельзя; администратор выберет окраску сам.
_FILE_NAMES = {"HE": HE, "H&E": HE, "AE": AE, "GOMORI": GOMORI, "CONGO": CONGO}


def from_file_name(token: str | None) -> str | None:
    return _FILE_NAMES.get((token or "").upper())


def from_free_name(name: str) -> str | None:
    """Настольная программа: имена файлов у врача свободные («Биопсия HE.svs»),
    поэтому окраска ищется отдельным словом в любом месте имени — из того же
    списка однозначных обозначений."""
    for word in re.split(r"[^\w&]+", name.rsplit(".", 1)[0]):
        code = from_file_name(word)
        if code:
            return code
    return None


def checked(code: str | None, marker: str | None) -> tuple[str | None, str | None]:
    """Значение из формы администратора → (код, маркер). Маркер остаётся только у ИГХ."""
    code = (code or "").strip().upper() or None
    if code is not None and code not in CODES:
        raise StainError("Неизвестная окраска")
    marker = " ".join((marker or "").split()) or None
    if code != IHC:
        return code, None
    if marker and len(marker) > MAX_MARKER_LENGTH:
        raise StainError(f"Маркер ИГХ не длиннее {MAX_MARKER_LENGTH} символов")
    return code, marker


def short(code: str | None, marker: str | None) -> str | None:
    """Подпись в названии скана: у ИГХ только маркер (ОК-2)."""
    if code == IHC and marker:
        return marker
    return SHORT.get(code) if code else None
