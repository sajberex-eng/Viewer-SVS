"""Аннотации на сканах: указатель на клетку и полигон (этап 9, раздел 13.6 ТЗ).

Геометрия хранится в координатах скана (пикселях полного разрешения), а не
экрана: пометка держится за ту же клетку при любом увеличении и повороте.

Кто что может (А-3):
  - ставит, правит и удаляет свои аннотации участник группы «Патологи»;
  - администратор правит и удаляет любые;
  - остальные, у кого есть доступ к скану, только видят.

Права на сам скан проверяет вызывающий код (`main.py`) — так же, как для
тайлов: аннотации чужого скана отвечают «не найден» (А-10).
"""
from __future__ import annotations

import json
import secrets

from .db import Database, utc_iso

PATHOLOGISTS = "Патологи"  # группа, участники которой размечают сканы
# tissue и artifact — контуры фрагментов ткани и артефактов для оценки клеточности
# (этап 11, КЛ-2): рисуются тем же инструментом, правятся так же, в списке
# аннотаций для обучения не показываются и не нумеруются.
KINDS = ("point", "polygon", "tissue", "artifact")
CONTOUR_KINDS = ("tissue", "artifact")
# Неоново-зелёный почти не встречается в окрашенных препаратах, поэтому он по
# умолчанию; ярко-красный — на случай зелёной окраски (решение заказчика 2026-09-20)
COLORS = ("green", "red")
MAX_POINTS = 500  # разумный предел на контур: дальше это уже не разметка
MAX_CONTOUR_POINTS = 4000  # контур фрагмента ткани по миниатюре бывает длинным
MAX_COMMENT = 1000
MIN_POLYGON_POINTS = 3


class AnnotationError(Exception):
    """Ошибка, понятная пользователю: показывается как есть."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def can_annotate(db: Database, user) -> bool:
    """Размечать могут администраторы и участники группы «Патологи»."""
    if user["role"] == "admin":
        return True
    row = db.query_one(
        """
        SELECT 1 FROM user_group_members m
          JOIN user_groups g ON g.id = m.group_id
         WHERE m.user_id = ? AND g.name = ? COLLATE NOCASE
        """,
        (user["id"], PATHOLOGISTS),
    )
    return row is not None


def _clean_points(kind: str, points) -> str:
    if kind not in KINDS:
        raise AnnotationError("Неизвестный вид аннотации")
    if not isinstance(points, list):
        raise AnnotationError("Координаты не переданы")
    need = 1 if kind == "point" else MIN_POLYGON_POINTS
    if len(points) < need:
        raise AnnotationError(
            "Укажите точку на скане" if kind == "point" else f"В контуре не меньше {MIN_POLYGON_POINTS} вершин"
        )
    limit = MAX_CONTOUR_POINTS if kind in CONTOUR_KINDS else MAX_POINTS
    if len(points) > limit:
        raise AnnotationError(f"В контуре не больше {limit} вершин")
    cleaned = []
    for pair in points:
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            raise AnnotationError("Координаты не переданы")
        try:
            x, y = float(pair[0]), float(pair[1])
        except (TypeError, ValueError):
            raise AnnotationError("Координаты не переданы") from None
        # Округление до десятых пикселя: точнее не нужно, а база меньше
        cleaned.append([round(x, 1), round(y, 1)])
    return json.dumps(cleaned, ensure_ascii=False)


def _clean_comment(comment: str | None) -> str:
    text = (comment or "").strip()
    if len(text) > MAX_COMMENT:
        raise AnnotationError(f"Комментарий не длиннее {MAX_COMMENT} символов")
    return text


def _clean_color(color: str | None) -> str:
    if color is None:
        return COLORS[0]
    if color not in COLORS:
        raise AnnotationError("Неизвестный цвет аннотации")
    return color


def as_dict(row, user) -> dict:
    """Аннотация для страницы. `can_edit` считается здесь, чтобы у клиента не
    было своей копии правил и они не разошлись."""
    return {
        "id": row["id"],
        "kind": row["kind"],
        "points": json.loads(row["points"]),
        "comment": row["comment"],
        "color": row["color"],
        "author": row["author"],
        "created_at": utc_iso(row["created_at"]),
        "updated_at": utc_iso(row["updated_at"]),
        "can_edit": can_edit(row, user),
    }


def can_edit(row, user) -> bool:
    """Свою правит автор, любую — администратор (А-3)."""
    return user["role"] == "admin" or row["author_id"] == user["id"]


def for_slide(db: Database, slide_id: str, user) -> list[dict]:
    rows = db.query(
        # rowid, а не id: время создания записывается с точностью до секунды, и
        # аннотации одной секунды иначе встают в случайном порядке
        "SELECT * FROM annotations WHERE slide_id = ? ORDER BY created_at, rowid", (slide_id,)
    )
    return [as_dict(row, user) for row in rows]


def contours(db: Database, slide_id: str):
    """Контуры для клеточности в порядке создания: ткань и артефакты (КЛ-2)."""
    return db.query(
        "SELECT * FROM annotations WHERE slide_id = ? AND kind IN ('tissue', 'artifact') ORDER BY created_at, rowid",
        (slide_id,),
    )


def get(db: Database, annotation_id: str):
    return db.query_one("SELECT * FROM annotations WHERE id = ?", (annotation_id,))


def create(db: Database, slide_id: str, kind: str, points, comment: str | None, user,
           color: str | None = None) -> dict:
    stored = _clean_points(kind, points)
    text = _clean_comment(comment)
    shade = _clean_color(color)
    annotation_id = secrets.token_hex(6)
    db.execute(
        """
        INSERT INTO annotations (id, slide_id, kind, points, comment, color, author_id, author)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (annotation_id, slide_id, kind, stored, text, shade, user["id"], user["login"]),
    )
    return as_dict(get(db, annotation_id), user)


def update(db: Database, row, points, comment: str | None, user, color: str | None = None) -> dict:
    """Правка без перерисовки (А-14): двигаются вершины, меняются комментарий и цвет."""
    stored = _clean_points(row["kind"], points) if points is not None else row["points"]
    text = _clean_comment(comment) if comment is not None else row["comment"]
    shade = _clean_color(color) if color is not None else row["color"]
    db.execute(
        "UPDATE annotations SET points = ?, comment = ?, color = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
        (stored, text, shade, row["id"]),
    )
    return as_dict(get(db, row["id"]), user)


def delete(db: Database, annotation_id: str) -> None:
    db.execute("DELETE FROM annotations WHERE id = ?", (annotation_id,))

