"""Тайл-сервер, каталог и веб-приложение вьювера."""
from __future__ import annotations

import io
import logging
import re
import sqlite3
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool
from starlette.middleware.sessions import SessionMiddleware

from . import annotations as annotationsvc
from . import audit
from . import groups as groupsvc
from .access import AccessIndex
from .auth import (
    LoginThrottle,
    account_problem,
    authenticate,
    generate_password,
    hash_password,
    require_admin,
    require_user,
    revoke_sessions,
    session_user,
    set_password,
    start_session,
    verify_password,
)
from .catalog import Catalog, CatalogError, slide_title
from .config import BASE_DIR, load_secret_key, load_settings
from .db import Database, utc_iso
from .perms import PermissionCache
from .slides import LABEL_IMAGE, SlidePool, render_thumbnail, thumbnail_path
from .storage import StorageUnavailable, create_storage
from .tilecache import TileCache, namespace as tile_namespace
from .uploads import PART_SIZE, UploadError, Uploads
from .warmup import Warmer

log = logging.getLogger(__name__)

WEB_DIR = BASE_DIR / "web"
# Ссылки на скрипты и стили внутри страницы: к ним дописывается версия
STATIC_LINK = re.compile(r'(?<=["\'])(/static/[^"\']+\.(?:js|css))(?=["\'])')
# immutable: пространство имён тайла включает размер и дату файла, поэтому по одному
# адресу всегда один и тот же тайл, и браузер не перепроверяет сотни адресов (СК-4)
TILE_CACHE_CONTROL = "private, max-age=604800, immutable"
LABEL_SIZE = (600, 600)

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "same-origin",  # ссылка на поле зрения не уходит на сторонние сайты
    "X-Robots-Tag": "noindex, nofollow",
    "Content-Security-Policy": (
        "default-src 'self'; img-src 'self' data: blob:; style-src 'self' 'unsafe-inline'; "
        "frame-ancestors 'none'"
    ),
}


class LoginRequest(BaseModel):
    login: str
    password: str


class PasswordRequest(BaseModel):
    current_password: str
    new_password: str


class FolderRequest(BaseModel):
    name: str
    parent_id: int | None = None


class FolderPatch(BaseModel):
    name: str | None = None
    parent_id: int | None = None
    move: bool = False  # отличает «перенести в корень» от «не трогать родителя»


class SlidePatch(BaseModel):
    title: str | None = None
    stain: str | None = None
    ihc_marker: str | None = None
    note: str | None = None
    folder_id: int | None = None


class AccessRequest(BaseModel):
    folder_id: int | None = None
    slide_id: str | None = None
    mode: str | None = None
    user_ids: list[int] = Field(default_factory=list)
    group_ids: list[int] = Field(default_factory=list)


class UserRequest(BaseModel):
    login: str
    name: str = ""
    role: str = "user"
    expires_at: str | None = None
    group_ids: list[int] = Field(default_factory=list)
    password: str | None = None  # пусто — пароль сгенерирует система


class PasswordReset(BaseModel):
    password: str | None = None


class UserPatch(BaseModel):
    name: str | None = None
    role: str | None = None
    status: str | None = None
    expires_at: str | None = None
    clear_expiry: bool = False
    group_ids: list[int] | None = None


class GroupRequest(BaseModel):
    name: str


class MembersRequest(BaseModel):
    user_ids: list[int] = Field(default_factory=list)


class UploadStart(BaseModel):
    folder_id: int
    name: str
    size: int


class UploadVerify(BaseModel):
    checksum: str


class AnnotationRequest(BaseModel):
    kind: str
    points: list[list[float]]
    comment: str = ""
    color: str | None = None


class AnnotationPatch(BaseModel):
    points: list[list[float]] | None = None
    comment: str | None = None
    color: str | None = None


def create_app() -> FastAPI:
    # Uvicorn настраивает только свои журналы, поэтому сообщения сервиса (прогрев,
    # проверка хранилища, уборка загрузок) до сих пор никуда не попадали: у корневого
    # журнала нет обработчика, и всё тише предупреждения терялось.
    if not logging.getLogger().handlers:
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    settings = load_settings()
    db = Database(settings.db_path)
    storage = create_storage(settings.storage)
    pool = SlidePool(storage, settings.tiles, settings.open_slides)
    catalog = Catalog(db, storage, pool, settings.check_minutes)
    uploads = Uploads(db, storage, catalog, settings.uploads_dir)
    catalog.on_periodic_check = uploads.cleanup_stale  # брошенные загрузки убираются сами
    tile_cache = TileCache(settings.tile_cache_dir, int(settings.cache.max_gb * 1e9))
    perms = PermissionCache(db)
    warmer = Warmer(pool, tile_cache, settings.tiles, settings.thumbs_dir)
    throttle = LoginThrottle()
    settings.thumbs_dir.mkdir(parents=True, exist_ok=True)

    def initial_check() -> None:
        try:
            log.info("Хранилище проверено: %s", catalog.check_integrity())
            uploads.cleanup_stale()
        except StorageUnavailable as exc:
            log.warning("Хранилище недоступно при запуске: %s", exc)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        tile_cache.start()
        warmer.start()
        threading.Thread(target=initial_check, name="storage-check", daemon=True).start()
        yield
        tile_cache.stop()
        warmer.stop()
        pool.close_all()
        db.close_all()

    app = FastAPI(title="Вьювер гистосканов", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.db = db
    app.state.perms = perms
    app.state.warmer = warmer
    app.add_middleware(
        SessionMiddleware,
        secret_key=load_secret_key(settings),
        session_cookie="viewer_session",
        max_age=settings.auth.session_hours * 3600,
        same_site="lax",
        https_only=settings.auth.https_only,
    )

    @app.middleware("http")
    async def drop_permission_cache(request: Request, call_next):
        """Любой изменяющий запрос сбрасывает память прав (СК-3).

        Так проверять не забыт ни один обработчик: отзыв доступа, блокировка,
        смена роли и перестройка папок действуют со следующего же запроса.
        Части загружаемого файла исключены: они идут по нескольку раз в секунду
        и на права не влияют.
        """
        try:
            return await call_next(request)
        finally:
            if request.method not in ("GET", "HEAD") and not request.url.path.startswith("/api/uploads"):
                perms.invalidate()

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        for name, value in SECURITY_HEADERS.items():
            response.headers.setdefault(name, value)
        return response

    @app.exception_handler(StorageUnavailable)
    async def storage_unavailable(request: Request, exc: StorageUnavailable):
        log.warning("Хранилище недоступно: %s", exc)
        return JSONResponse(
            status_code=503,
            content={"detail": "Хранилище сканов временно недоступно. Попробуйте позже или сообщите администратору."},
        )

    @app.exception_handler(CatalogError)
    async def catalog_error(request: Request, exc: CatalogError):
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    @app.exception_handler(UploadError)
    async def upload_error(request: Request, exc: UploadError):
        return JSONResponse(status_code=exc.status, content={"detail": str(exc)})

    @app.exception_handler(annotationsvc.AnnotationError)
    async def annotation_error(request: Request, exc: annotationsvc.AnnotationError):
        return JSONResponse(status_code=exc.status, content={"detail": str(exc)})

    class FreshStatic(StaticFiles):
        """Статика с обязательной перепроверкой версии.

        Без этого браузер держит в кэше прежние скрипты, и после обновления
        сервиса часть пользователей продолжает работать со старым кодом.
        Файл всё равно передаётся один раз: при совпадении ETag ответ пустой.
        """

        def file_response(self, *args, **kwargs):
            response = super().file_response(*args, **kwargs)
            response.headers["Cache-Control"] = "no-cache"
            return response

    app.mount("/static", FreshStatic(directory=WEB_DIR / "static"), name="static")

    # ---------- представление данных ----------

    def access_for(user) -> AccessIndex:
        return perms.index(user)

    def slide_summary(row, user) -> dict:
        # Ключ и исходное имя файла обычному пользователю не отдаются:
        # в имени могут оказаться персональные данные.
        info = {
            "id": row["id"],
            "title": slide_title(row),
            "folder_id": row["folder_id"],
            "stain": row["stain"],           # код из stains.CODES
            "ihc_marker": row["ihc_marker"],
            "note": row["note"],
            "width": row["width"],
            "height": row["height"],
            "objective": row["objective"],
            "mpp": row["mpp"],
            # Формат по расширению внутреннего ключа (SVS, KFB…): имени файла в нём нет
            "format": Path(row["key"]).suffix.lstrip(".").upper(),
            "has_label": bool(row["has_label"]),
            "added_at": utc_iso(row["added_at"]),
        }
        if user["role"] == "admin":
            info["original_name"] = row["original_name"]
            info["access_mode"] = row["access_mode"]
            info["size_bytes"] = row["size"]  # врачу размер файла не нужен (ИН-11)
        return info

    def folder_summary(row, user) -> dict:
        info = {"id": row["id"], "name": row["name"], "parent_id": row["parent_id"]}
        if user["role"] == "admin":
            info["access_mode"] = row["access_mode"]
        return info

    def require_slide(slide_id: str, user):
        """Скан, доступный этому пользователю. Иначе 404: чужой скан неотличим от несуществующего."""
        row = catalog.visible_slide(slide_id, access_for(user))
        if row is None:
            raise HTTPException(404, "Скан не найден")
        return row

    # ---------- страницы ----------

    # Версия статики: меняется при любом изменении скриптов и стилей и
    # подставляется в ссылки на странице. Без этого браузер держит модули в
    # кэше, и новая страница выполняется со старым кодом.
    assets_version = str(int(max(
        (path.stat().st_mtime for path in (WEB_DIR / "static").rglob("*") if path.is_file()),
        default=0,
    )))

    def page(name: str) -> Response:
        html = (WEB_DIR / "pages" / name).read_text(encoding="utf-8")
        html = STATIC_LINK.sub(lambda match: f"{match.group(1)}?v={assets_version}", html)
        return Response(html, media_type="text/html", headers={"Cache-Control": "no-cache"})

    def protected_page(request: Request, name: str):
        if session_user(request) is None:
            target = request.url.path + (f"?{request.url.query}" if request.url.query else "")
            return RedirectResponse(f"/login?next={quote(target, safe='')}", status_code=303)
        return page(name)

    @app.get("/login")
    def login_page():
        return page("login.html")

    @app.get("/favicon.ico")
    def favicon():
        """Значок сайта (ИН-14): браузеры и закладки спрашивают его по этому адресу,
        даже когда на странице указан свой. Отдаётся логотип Центра."""
        return FileResponse(
            WEB_DIR / "static" / "img" / "logo.svg",
            media_type="image/svg+xml",
            headers={"Cache-Control": "public, max-age=86400"},
        )

    @app.get("/")
    def catalog_page(request: Request):
        return protected_page(request, "index.html")

    @app.get("/viewer")
    def viewer_page(request: Request):
        return protected_page(request, "viewer.html")

    @app.get("/admin")
    def admin_page(request: Request):
        # Данные отдаёт API, и каждый его маршрут требует роль администратора;
        # страница сама по себе ничего не раскрывает.
        return protected_page(request, "admin.html")

    @app.get("/robots.txt")
    def robots():
        return Response("User-agent: *\nDisallow: /\n", media_type="text/plain")

    # ---------- вход ----------

    @app.post("/api/login")
    def login(body: LoginRequest, request: Request):
        login_name = body.login.strip()
        key = f"{login_name.lower()}|{audit.client_ip(request)}"
        if throttle.blocked(key):
            raise HTTPException(429, "Слишком много неудачных попыток. Повторите через несколько минут.")
        user = authenticate(db, login_name, body.password)
        if user is None:
            throttle.record_failure(key)
            audit.log(db, request, audit.LOGIN_FAILED, actor=login_name)
            raise HTTPException(401, "Неверный логин или пароль")
        problem = account_problem(user)
        if problem:
            throttle.record_failure(key)
            audit.log(db, request, audit.LOGIN_FAILED, user=user, detail="учётная запись недоступна")
            raise HTTPException(403, problem)
        throttle.reset(key)
        start_session(request, user)
        db.execute("UPDATE users SET last_login_at = CURRENT_TIMESTAMP WHERE id = ?", (user["id"],))
        audit.log(db, request, audit.LOGIN_OK, user=user)
        return {"login": user["login"], "role": user["role"], "name": user["name"]}

    @app.post("/api/logout")
    def logout(request: Request):
        user = session_user(request)
        if user is not None:
            audit.log(db, request, audit.LOGOUT, user=user)
        request.session.clear()
        return {"ok": True}

    @app.get("/api/me")
    def me(user=Depends(require_user)):
        return {"login": user["login"], "role": user["role"], "name": user["name"]}

    @app.post("/api/password")
    def change_password(body: PasswordRequest, request: Request, user=Depends(require_user)):
        if not verify_password(body.current_password, user["password_hash"]):
            raise HTTPException(400, "Текущий пароль указан неверно")
        try:
            set_password(db, user["id"], body.new_password)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        audit.log(db, request, audit.PASSWORD_CHANGED, user=user)
        # Пароль сменён: сессия получает новую версию, прочие сессии этого пользователя обрываются
        start_session(request, db.query_one("SELECT * FROM users WHERE id = ?", (user["id"],)))
        return {"ok": True}

    # ---------- каталог ----------

    @app.get("/api/catalog")
    def catalog_tree(user=Depends(require_user)):
        """Папки и сканы, доступные этому пользователю."""
        catalog.check_if_stale()
        access = access_for(user)
        slides = catalog.slides()
        visible_slides = [row for row in slides if access.can_view_slide(row)]
        visible_folders = access.visible_folder_ids(slides)
        space = None
        if user["role"] == "admin":
            disk = storage.space()
            space = {
                "free_bytes": disk.free,
                "total_bytes": disk.total,
                "warn_below_bytes": settings.storage.warn_free_gb * 1e9,
            }
        return {
            "folders": [folder_summary(row, user) for row in catalog.folders() if row["id"] in visible_folders],
            "slides": [slide_summary(row, user) for row in visible_slides],
            "space": space,
        }

    @app.get("/api/slides/{slide_id}")
    def slide_info(slide_id: str, request: Request, user=Depends(require_user)):
        row = require_slide(slide_id, user)
        audit.log(db, request, audit.SLIDE_OPEN, user=user, object_type="slide", object_id=slide_id)
        info = slide_summary(row, user)
        info["tiles"] = {
            "url": f"/api/slides/{slide_id}/tiles/",
            "tile_size": settings.tiles.tile_size,
            "overlap": settings.tiles.overlap,
            "format": "jpg",
        }
        info["can_annotate"] = annotationsvc.can_annotate(db, user)  # А-3
        access = access_for(user)
        # Путь до папки: во вьювере он показан рядом с названием и открывает
        # эту папку в каталоге (ИН-3). Папки выше доступного скана пользователю
        # видны по определению: они и делают скан достижимым.
        names = {folder["id"]: folder["name"] for folder in catalog.folders()}
        info["path"] = [
            {"id": folder_id, "name": names[folder_id]}
            for folder_id in access.ancestors(row["folder_id"])
            if folder_id in names
        ]
        info["siblings"] = [
            {"id": s["id"], "title": slide_title(s)}
            for s in catalog.slides(row["folder_id"])
            if access.can_view_slide(s)
        ]
        return info

    # ---------- изображения ----------

    @app.get("/api/slides/{slide_id}/tiles/{level:int}/{col:int}_{row:int}.jpg")
    def tile(slide_id: str, level: int, col: int, row: int, user=Depends(require_user)):
        slide_row = require_slide(slide_id, user)
        cfg = settings.tiles
        path = tile_cache.path(tile_namespace(slide_row, cfg), level, col, row)
        headers = {"Cache-Control": TILE_CACHE_CONTROL}
        if tile_cache.get(path):
            return FileResponse(path, media_type="image/jpeg", headers=headers)

        with pool.acquire(slide_row["key"]) as handle:
            tiler = handle.tiler
            if not 0 <= level < tiler.level_count:
                raise HTTPException(404, "Нет такого уровня")
            cols, rows = tiler.tile_count(level)
            if not (0 <= col < cols and 0 <= row < rows):
                raise HTTPException(404, "Нет такого тайла")
            try:
                image = tiler.get_tile(level, col, row)
            except Exception as exc:  # OpenSlide сообщает о сбое чтения разными исключениями
                raise StorageUnavailable(f"Ошибка чтения тайла: {exc}") from exc
        buffer = io.BytesIO()
        image.save(buffer, "JPEG", quality=cfg.jpeg_quality)
        data = buffer.getvalue()
        tile_cache.put(path, data)
        return Response(data, media_type="image/jpeg", headers=headers)

    @app.get("/api/slides/{slide_id}/thumbnail.jpg")
    def thumbnail(slide_id: str, user=Depends(require_user)):
        slide_row = require_slide(slide_id, user)
        path = thumbnail_path(settings.thumbs_dir, slide_row)
        if not path.exists():  # обычно её уже приготовил прогрев после загрузки (СК-1)
            with pool.acquire(slide_row["key"]) as handle:
                render_thumbnail(handle.slide, path)
        return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})

    @app.get("/api/slides/{slide_id}/label.jpg")
    def label(slide_id: str, request: Request, user=Depends(require_user)):
        """Этикетка стекла: на ней бывают персональные данные, поэтому без кэша в браузере."""
        slide_row = require_slide(slide_id, user)
        if not slide_row["has_label"]:
            raise HTTPException(404, "У этого скана нет этикетки")
        with pool.acquire(slide_row["key"]) as handle:
            try:
                image = handle.slide.associated_images[LABEL_IMAGE].convert("RGB")
            except Exception as exc:
                raise StorageUnavailable(f"Ошибка чтения этикетки: {exc}") from exc
        image.thumbnail(LABEL_SIZE)
        buffer = io.BytesIO()
        image.save(buffer, "JPEG", quality=88)
        audit.log(db, request, audit.SLIDE_LABEL, user=user, object_type="slide", object_id=slide_id)
        return Response(
            buffer.getvalue(),
            media_type="image/jpeg",
            headers={"Cache-Control": "no-store"},
        )

    # ---------- аннотации (этап 9) ----------

    def require_annotation(annotation_id: str, user):
        """Аннотация доступного скана. Иначе 404, как для тайлов (А-10)."""
        row = annotationsvc.get(db, annotation_id)
        if row is None or catalog.visible_slide(row["slide_id"], access_for(user)) is None:
            raise HTTPException(404, "Аннотация не найдена")
        return row

    @app.get("/api/slides/{slide_id}/annotations")
    def list_annotations(slide_id: str, user=Depends(require_user)):
        require_slide(slide_id, user)  # чужой скан — 404, вместе с его аннотациями
        return annotationsvc.for_slide(db, slide_id, user)

    @app.post("/api/slides/{slide_id}/annotations")
    def create_annotation(slide_id: str, body: AnnotationRequest, request: Request, user=Depends(require_user)):
        require_slide(slide_id, user)
        if not annotationsvc.can_annotate(db, user):
            raise HTTPException(403, "Размечать сканы могут патологи")
        created = annotationsvc.create(db, slide_id, body.kind, body.points, body.comment, user, body.color)
        audit.log(
            db, request, audit.ANNOTATION_CREATE, user=user, object_type="annotation",
            object_id=created["id"], detail=f"{body.kind}, скан {slide_id}",  # текст комментария не пишем (А-8)
        )
        return created

    @app.patch("/api/annotations/{annotation_id}")
    def patch_annotation(annotation_id: str, body: AnnotationPatch, request: Request, user=Depends(require_user)):
        row = require_annotation(annotation_id, user)
        if not annotationsvc.can_annotate(db, user) or not annotationsvc.can_edit(row, user):
            raise HTTPException(403, "Чужую аннотацию изменить нельзя")
        updated = annotationsvc.update(db, row, body.points, body.comment, user, body.color)
        audit.log(
            db, request, audit.ANNOTATION_UPDATE, user=user, object_type="annotation",
            object_id=annotation_id, detail=f"{row['kind']}, скан {row['slide_id']}",
        )
        return updated

    @app.delete("/api/annotations/{annotation_id}")
    def delete_annotation(annotation_id: str, request: Request, user=Depends(require_user)):
        row = require_annotation(annotation_id, user)
        # Свою удаляет автор-патолог, любую — администратор (А-3)
        allowed = user["role"] == "admin" or (annotationsvc.can_annotate(db, user) and row["author_id"] == user["id"])
        if not allowed:
            raise HTTPException(403, "Чужую аннотацию удалить нельзя")
        annotationsvc.delete(db, annotation_id)
        audit.log(
            db, request, audit.ANNOTATION_DELETE, user=user, object_type="annotation",
            object_id=annotation_id, detail=f"{row['kind']}, скан {row['slide_id']}",
        )
        return {"ok": True}

    # ---------- папки: только администратор ----------

    @app.post("/api/folders")
    def create_folder(body: FolderRequest, request: Request, user=Depends(require_admin)):
        folder_id = catalog.create_folder(body.name, body.parent_id, user)
        audit.log(db, request, audit.FOLDER_CREATE, user=user, object_type="folder", object_id=folder_id)
        return {"id": folder_id}

    @app.patch("/api/folders/{folder_id}")
    def patch_folder(folder_id: int, body: FolderPatch, request: Request, user=Depends(require_admin)):
        if body.name is not None:
            catalog.rename_folder(folder_id, body.name)
            audit.log(db, request, audit.FOLDER_RENAME, user=user, object_type="folder", object_id=folder_id)
        if body.move:
            catalog.move_folder(folder_id, body.parent_id)
            audit.log(db, request, audit.FOLDER_MOVE, user=user, object_type="folder", object_id=folder_id)
        return {"ok": True}

    @app.get("/api/folders/{folder_id}/contents")
    def folder_contents(folder_id: int, user=Depends(require_admin)):
        folders, slides = catalog.folder_contents_count(folder_id)
        return {"folders": folders, "slides": slides}

    @app.delete("/api/folders/{folder_id}")
    def delete_folder(folder_id: int, request: Request, user=Depends(require_admin)):
        folders, slides = catalog.delete_folder(folder_id)
        audit.log(
            db, request, audit.FOLDER_DELETE, user=user, object_type="folder", object_id=folder_id,
            detail=f"папок: {folders}, сканов: {slides}",
        )
        return {"folders": folders, "slides": slides}

    # ---------- сканы: только администратор ----------

    @app.patch("/api/slides/{slide_id}")
    def patch_slide(slide_id: str, body: SlidePatch, request: Request, user=Depends(require_admin)):
        if body.folder_id is not None:
            catalog.move_slide(slide_id, body.folder_id)
            audit.log(db, request, audit.SLIDE_MOVE, user=user, object_type="slide", object_id=slide_id)
        if body.title is not None or body.stain is not None or body.note is not None:
            catalog.update_slide(slide_id, title=body.title, stain=body.stain,
                                 ihc_marker=body.ihc_marker, note=body.note)
            audit.log(db, request, audit.SLIDE_UPDATE, user=user, object_type="slide", object_id=slide_id)
        return {"ok": True}

    @app.delete("/api/slides/{slide_id}")
    def delete_slide(slide_id: str, request: Request, user=Depends(require_admin)):
        catalog.delete_slide(slide_id)
        audit.log(db, request, audit.SLIDE_DELETE, user=user, object_type="slide", object_id=slide_id)
        return {"ok": True}

    @app.post("/api/storage/check")
    def storage_check(user=Depends(require_admin)):
        return {**catalog.check_integrity(), "uploads": uploads.cleanup_stale()}

    # ---------- доступ ----------

    @app.get("/api/access")
    def get_access(folder_id: int | None = None, slide_id: str | None = None, user=Depends(require_admin)):
        access = access_for(user)
        if folder_id is not None:
            row = catalog.folder(folder_id)
            if row is None:
                raise HTTPException(404, "Папка не найдена")
            source = access.folder_source(row["parent_id"]) if row["access_mode"] is None else None
            return {
                "mode": row["access_mode"],
                "user_ids": catalog.granted_user_ids(folder_id=folder_id),
                "group_ids": catalog.granted_group_ids(folder_id=folder_id),
                "inherited_from": source.id if source else None,
                "inherited_mode": source.mode if source else None,
            }
        row = catalog.slide(slide_id)
        if row is None:
            raise HTTPException(404, "Скан не найден")
        source = access.folder_source(row["folder_id"]) if row["access_mode"] is None else None
        return {
            "mode": row["access_mode"],
            "user_ids": catalog.granted_user_ids(slide_id=slide_id),
            "group_ids": catalog.granted_group_ids(slide_id=slide_id),
            "inherited_from": source.id if source else None,
            "inherited_mode": source.mode if source else None,
        }

    @app.post("/api/access")
    def set_access(body: AccessRequest, request: Request, user=Depends(require_admin)):
        catalog.set_access(
            folder_id=body.folder_id, slide_id=body.slide_id, mode=body.mode,
            user_ids=body.user_ids, group_ids=body.group_ids, actor=user,
        )
        audit.log(
            db, request, audit.ACCESS_CHANGE, user=user,
            object_type="folder" if body.folder_id is not None else "slide",
            object_id=body.folder_id if body.folder_id is not None else body.slide_id,
            detail=f"режим: {body.mode or 'наследуется'}",
        )
        return {"ok": True}

    @app.get("/api/access/preview/{user_id}")
    def access_preview(user_id: int, admin=Depends(require_admin)):
        """Что видит выбранный пользователь (ТЗ Д-7)."""
        target = db.query_one("SELECT * FROM users WHERE id = ?", (user_id,))
        if target is None:
            raise HTTPException(404, "Пользователь не найден")
        access = AccessIndex(db, target)
        slides = catalog.slides()
        visible = [row for row in slides if access.can_view_slide(row)]
        folder_ids = access.visible_folder_ids(slides)
        names = {row["id"]: row["name"] for row in catalog.folders()}
        return {
            "folders": [{"id": fid, "name": names.get(fid, "")} for fid in sorted(folder_ids)],
            "slides": [{"id": row["id"], "title": slide_title(row)} for row in visible],
        }

    # ---------- пользователи ----------

    def user_summary(row, memberships: dict[int, list[int]]) -> dict:
        return {
            "id": row["id"],
            "login": row["login"],
            "name": row["name"],
            "role": row["role"],
            "status": row["status"],
            "expires_at": row["expires_at"],
            "group_ids": memberships.get(row["id"], []),
            "last_login_at": utc_iso(row["last_login_at"]),
        }

    def memberships() -> dict[int, list[int]]:
        result: dict[int, list[int]] = {}
        for row in db.query("SELECT group_id, user_id FROM user_group_members ORDER BY group_id"):
            result.setdefault(row["user_id"], []).append(row["group_id"])
        return result

    def admin_count() -> int:
        return db.query_one("SELECT count(*) AS n FROM users WHERE role = 'admin' AND status = 'active'")["n"]

    @app.get("/api/users")
    def list_users(user=Depends(require_admin)):
        groups_of = memberships()
        return [user_summary(row, groups_of) for row in db.query("SELECT * FROM users ORDER BY login")]

    @app.post("/api/users")
    def create_user(body: UserRequest, request: Request, user=Depends(require_admin)):
        login_name = body.login.strip()
        if not login_name:
            raise HTTPException(400, "Укажите логин")
        if body.role not in ("user", "admin"):
            raise HTTPException(400, "Неизвестная роль")
        known_groups = {g["id"] for g in groupsvc.list_groups(db)}
        if not set(body.group_ids) <= known_groups:
            raise HTTPException(400, "Группа не найдена")
        # Администратор может задать пароль сам; пустое поле означает «сгенерировать»
        password = (body.password or "").strip() or generate_password()
        try:
            password_hash = hash_password(password)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        try:
            user_id = db.insert(
                "INSERT INTO users (login, password_hash, role, name, expires_at) VALUES (?, ?, ?, ?, ?)",
                (login_name, password_hash, body.role, body.name.strip() or login_name, body.expires_at),
            )
        except sqlite3.IntegrityError as exc:
            raise HTTPException(400, "Пользователь с таким логином уже есть") from exc
        groupsvc.set_user_groups(db, user_id, body.group_ids)
        audit.log(db, request, audit.USER_CREATE, user=user, object_type="user", object_id=user_id, detail=login_name)
        # Пароль показывается администратору один раз и нигде не сохраняется
        return {"id": user_id, "login": login_name, "password": password}

    @app.patch("/api/users/{user_id}")
    def patch_user(user_id: int, body: UserPatch, request: Request, user=Depends(require_admin)):
        target = db.query_one("SELECT * FROM users WHERE id = ?", (user_id,))
        if target is None:
            raise HTTPException(404, "Пользователь не найден")
        role = body.role or target["role"]
        status = body.status or target["status"]
        if role not in ("user", "admin") or status not in ("active", "blocked"):
            raise HTTPException(400, "Недопустимое значение")
        losing_admin = target["role"] == "admin" and target["status"] == "active" and (
            role != "admin" or status != "active"
        )
        if losing_admin and admin_count() <= 1:
            raise HTTPException(400, "Это последний администратор")
        if target["id"] == user["id"] and status == "blocked":
            raise HTTPException(400, "Нельзя заблокировать самого себя")
        expires_at = None if body.clear_expiry else (body.expires_at or target["expires_at"])
        db.execute(
            "UPDATE users SET name = ?, role = ?, status = ?, expires_at = ? WHERE id = ?",
            (body.name if body.name is not None else target["name"], role, status, expires_at, user_id),
        )
        if body.group_ids is not None:
            groupsvc.set_user_groups(db, user_id, body.group_ids)
        if status == "blocked" or role != target["role"]:
            revoke_sessions(db, user_id)  # изменение действует сразу
        audit.log(db, request, audit.USER_UPDATE, user=user, object_type="user", object_id=user_id)
        return {"ok": True}

    @app.post("/api/users/{user_id}/password")
    def reset_password(user_id: int, body: PasswordReset, request: Request, user=Depends(require_admin)):
        target = db.query_one("SELECT * FROM users WHERE id = ?", (user_id,))
        if target is None:
            raise HTTPException(404, "Пользователь не найден")
        password = (body.password or "").strip() or generate_password()
        try:
            set_password(db, user_id, password)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        audit.log(db, request, audit.USER_RESET_PASSWORD, user=user, object_type="user", object_id=user_id)
        return {"login": target["login"], "password": password}

    @app.delete("/api/users/{user_id}")
    def delete_user(user_id: int, request: Request, user=Depends(require_admin)):
        target = db.query_one("SELECT * FROM users WHERE id = ?", (user_id,))
        if target is None:
            raise HTTPException(404, "Пользователь не найден")
        if target["id"] == user["id"]:
            raise HTTPException(400, "Нельзя удалить самого себя")
        if target["role"] == "admin" and admin_count() <= 1:
            raise HTTPException(400, "Это последний администратор")
        db.execute("DELETE FROM users WHERE id = ?", (user_id,))
        audit.log(
            db, request, audit.USER_DELETE, user=user, object_type="user",
            object_id=user_id, detail=target["login"],
        )
        return {"ok": True}

    # ---------- группы ----------

    @app.get("/api/groups")
    def list_groups(user=Depends(require_admin)):
        return {"groups": groupsvc.list_groups(db), "administrators": groupsvc.administrators(db)}

    @app.post("/api/groups")
    def create_group(body: GroupRequest, request: Request, user=Depends(require_admin)):
        group_id = groupsvc.create_group(db, body.name)
        audit.log(db, request, audit.GROUP_CREATE, user=user, object_type="group", object_id=group_id, detail=body.name)
        return {"id": group_id}

    @app.patch("/api/groups/{group_id}")
    def rename_group(group_id: int, body: GroupRequest, request: Request, user=Depends(require_admin)):
        groupsvc.rename_group(db, group_id, body.name)
        audit.log(db, request, audit.GROUP_RENAME, user=user, object_type="group", object_id=group_id)
        return {"ok": True}

    @app.put("/api/groups/{group_id}/members")
    def set_group_members(group_id: int, body: MembersRequest, request: Request, user=Depends(require_admin)):
        groupsvc.set_members(db, group_id, body.user_ids)
        audit.log(
            db, request, audit.GROUP_MEMBERS, user=user, object_type="group", object_id=group_id,
            detail=f"участников: {len(set(body.user_ids))}",
        )
        return {"ok": True}

    @app.delete("/api/groups/{group_id}")
    def delete_group(group_id: int, request: Request, user=Depends(require_admin)):
        groupsvc.delete_group(db, group_id)
        audit.log(db, request, audit.GROUP_DELETE, user=user, object_type="group", object_id=group_id)
        return {"ok": True}

    # ---------- загрузка сканов: только администратор ----------

    @app.get("/api/uploads")
    def pending_uploads(user=Depends(require_admin)):
        """Незавершённые загрузки этого администратора: после обрыва связи их можно продолжить."""
        return uploads.pending(user)

    @app.post("/api/uploads")
    def start_upload(body: UploadStart, request: Request, user=Depends(require_admin)):
        state = uploads.start(body.folder_id, body.name, body.size, user)
        if state["received"] == 0:
            audit.log(
                db, request, audit.UPLOAD_START, user=user, object_type="upload", object_id=state["id"],
                detail=f"{body.size / 1e6:.0f} МБ",  # имя файла в журнал не пишется
            )
        started_by = state.pop("started_by", None)
        if started_by is not None:  # чужую загрузку продолжает другой администратор (ЗГ-4)
            row = db.query_one("SELECT login FROM users WHERE id = ?", (started_by,))
            audit.log(
                db, request, audit.UPLOAD_RESUME, user=user, object_type="upload", object_id=state["id"],
                detail=f"начал: {row['login'] if row else 'удалённый пользователь'}",
            )
        return state

    @app.post("/api/uploads/{upload_id}/verify")
    async def verify_upload(upload_id: str, body: UploadVerify, user=Depends(require_admin)):
        """Тот ли это файл: сверка последней принятой части (ЗГ-5)."""
        return await run_in_threadpool(uploads.verify_tail, upload_id, body.checksum)

    @app.put("/api/uploads/{upload_id}")
    async def upload_part(upload_id: str, offset: int, request: Request, user=Depends(require_admin)):
        length = int(request.headers.get("content-length") or 0)
        if length <= 0 or length > 2 * PART_SIZE:
            raise HTTPException(413, "Часть файла слишком большая или пустая")
        data = await request.body()
        checksum = request.headers.get("x-part-sha256")
        return await run_in_threadpool(uploads.accept_part, upload_id, offset, data, checksum, user)

    @app.post("/api/uploads/{upload_id}/complete")
    def complete_upload(upload_id: str, request: Request, user=Depends(require_admin)):
        slide_id = uploads.complete(upload_id, user)
        audit.log(db, request, audit.SLIDE_UPLOAD, user=user, object_type="slide", object_id=slide_id)
        warmer.enqueue(catalog.slide(slide_id))  # миниатюра и обзорные тайлы готовятся в фоне (СК-1)
        return {"slide_id": slide_id}

    @app.delete("/api/uploads/{upload_id}")
    def cancel_upload(upload_id: str, user=Depends(require_admin)):
        uploads.cancel(upload_id, user)
        return {"ok": True}

    # ---------- журнал ----------

    @app.get("/api/journal")
    def journal(limit: int = 200, offset: int = 0, action: str | None = None,
                actor: str | None = None, user=Depends(require_admin)):
        limit = max(1, min(limit, 1000))
        where, params = [], []
        if action:
            where.append("action = ?")
            params.append(action)
        if actor:
            where.append("actor = ?")
            params.append(actor)
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        rows = db.query(
            f"SELECT * FROM audit_log {clause} ORDER BY id DESC LIMIT ? OFFSET ?",
            (*params, limit, offset),
        )
        return [
            {
                "at": utc_iso(row["at"]), "actor": row["actor"], "action": row["action"],
                "object_type": row["object_type"], "object_id": row["object_id"],
                "detail": row["detail"], "ip": row["ip"],
            }
            for row in rows
        ]

    return app


app = create_app()
