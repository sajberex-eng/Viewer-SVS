"""Группы пользователей: «Патологи», «Гематологи», «Резиденты» и любые другие.

Пользователь может входить в несколько групп. «Администраторы» группой в базе
не является: это роль admin, у неё доступ есть всегда, поэтому в списках групп
она показывается отдельной подписью и не редактируется.
"""
from __future__ import annotations

import sqlite3

from .catalog import CatalogError, MAX_NAME_LENGTH
from .db import Database

ADMINISTRATORS_LABEL = "Администраторы"


def _clean_name(name: str) -> str:
    name = " ".join(name.split())
    if not name:
        raise CatalogError("Название группы не может быть пустым")
    if len(name) > MAX_NAME_LENGTH:
        raise CatalogError(f"Название группы не длиннее {MAX_NAME_LENGTH} символов")
    if name.casefold() == ADMINISTRATORS_LABEL.casefold():
        raise CatalogError("Название «Администраторы» зарезервировано: это роль администратора")
    return name


def list_groups(db: Database) -> list[dict]:
    members: dict[int, list[int]] = {}
    for row in db.query("SELECT group_id, user_id FROM user_group_members ORDER BY user_id"):
        members.setdefault(row["group_id"], []).append(row["user_id"])
    return [
        {"id": row["id"], "name": row["name"], "member_ids": members.get(row["id"], [])}
        for row in db.query("SELECT id, name FROM user_groups ORDER BY name COLLATE NOCASE")
    ]


def administrators(db: Database) -> dict:
    ids = [r["id"] for r in db.query("SELECT id FROM users WHERE role = 'admin' AND status = 'active' ORDER BY id")]
    return {"name": ADMINISTRATORS_LABEL, "member_ids": ids, "builtin": True}


def get_group(db: Database, group_id: int):
    row = db.query_one("SELECT * FROM user_groups WHERE id = ?", (group_id,))
    if row is None:
        raise CatalogError("Группа не найдена")
    return row


def _check_name_free(db: Database, name: str, exclude: int | None = None) -> None:
    """Регистр не различается. Делаем это здесь, а не в базе: NOCASE в SQLite знает только латиницу."""
    target = name.casefold()
    for row in db.query("SELECT id, name FROM user_groups"):
        if row["id"] != exclude and row["name"].casefold() == target:
            raise CatalogError("Группа с таким названием уже есть")


def create_group(db: Database, name: str) -> int:
    name = _clean_name(name)
    _check_name_free(db, name)
    try:
        return db.insert("INSERT INTO user_groups (name) VALUES (?)", (name,))
    except sqlite3.IntegrityError as exc:
        raise CatalogError("Группа с таким названием уже есть") from exc


def rename_group(db: Database, group_id: int, name: str) -> None:
    get_group(db, group_id)
    name = _clean_name(name)
    _check_name_free(db, name, exclude=group_id)
    try:
        db.execute("UPDATE user_groups SET name = ? WHERE id = ?", (name, group_id))
    except sqlite3.IntegrityError as exc:
        raise CatalogError("Группа с таким названием уже есть") from exc


def delete_group(db: Database, group_id: int) -> None:
    """Удаляет группу вместе с её разрешениями: доступ, выданный через неё, пропадает сразу."""
    get_group(db, group_id)
    db.execute("DELETE FROM user_groups WHERE id = ?", (group_id,))


def set_members(db: Database, group_id: int, user_ids: list[int]) -> None:
    get_group(db, group_id)
    wanted = list(dict.fromkeys(user_ids))
    if wanted:
        placeholders = ",".join("?" * len(wanted))
        found = db.query(f"SELECT id FROM users WHERE id IN ({placeholders})", tuple(wanted))
        if len(found) != len(wanted):
            raise CatalogError("Пользователь не найден")
    with db.transaction() as conn:
        conn.execute("DELETE FROM user_group_members WHERE group_id = ?", (group_id,))
        conn.executemany(
            "INSERT INTO user_group_members (group_id, user_id) VALUES (?, ?)",
            [(group_id, uid) for uid in wanted],
        )


def set_user_groups(db: Database, user_id: int, group_ids: list[int]) -> None:
    wanted = list(dict.fromkeys(group_ids))
    if wanted:
        placeholders = ",".join("?" * len(wanted))
        found = db.query(f"SELECT id FROM user_groups WHERE id IN ({placeholders})", tuple(wanted))
        if len(found) != len(wanted):
            raise CatalogError("Группа не найдена")
    with db.transaction() as conn:
        conn.execute("DELETE FROM user_group_members WHERE user_id = ?", (user_id,))
        conn.executemany(
            "INSERT INTO user_group_members (group_id, user_id) VALUES (?, ?)",
            [(gid, user_id) for gid in wanted],
        )
