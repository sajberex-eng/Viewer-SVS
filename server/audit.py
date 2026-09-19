"""Журнал действий (ТЗ Д-8).

Хранится не менее года и переживает удаление пользователя, поэтому логин
записывается текстом. Исходные имена файлов и пароли в журнал не попадают.
"""
from __future__ import annotations

from fastapi import Request

from .db import Database

# Действия. Названия видны только в коде, в интерфейс идут подписи из i18n.
LOGIN_OK = "login.ok"
LOGIN_FAILED = "login.failed"
LOGOUT = "logout"
PASSWORD_CHANGED = "password.changed"
SLIDE_OPEN = "slide.open"
SLIDE_LABEL = "slide.label"
SLIDE_UPLOAD = "slide.upload"
SLIDE_UPDATE = "slide.update"
SLIDE_MOVE = "slide.move"
SLIDE_DELETE = "slide.delete"
FOLDER_CREATE = "folder.create"
FOLDER_RENAME = "folder.rename"
FOLDER_MOVE = "folder.move"
FOLDER_DELETE = "folder.delete"
ACCESS_CHANGE = "access.change"
USER_CREATE = "user.create"
USER_UPDATE = "user.update"
USER_DELETE = "user.delete"
USER_RESET_PASSWORD = "user.reset_password"


def client_ip(request: Request) -> str:
    return request.client.host if request.client else ""


def log(
    db: Database,
    request: Request | None,
    action: str,
    *,
    user=None,
    actor: str = "",
    object_type: str | None = None,
    object_id: str | int | None = None,
    detail: str | None = None,
) -> None:
    db.execute(
        """
        INSERT INTO audit_log (user_id, actor, action, object_type, object_id, detail, ip)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            user["id"] if user is not None else None,
            actor or (user["login"] if user is not None else ""),
            action,
            object_type,
            None if object_id is None else str(object_id),
            detail,
            client_ip(request) if request is not None else "",
        ),
    )
