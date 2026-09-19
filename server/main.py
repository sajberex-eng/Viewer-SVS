"""Тайл-сервер и веб-приложение вьювера."""
from __future__ import annotations

import io
import logging
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.middleware.sessions import SessionMiddleware

from .auth import LoginThrottle, authenticate, require_admin, require_user, session_user
from .config import BASE_DIR, load_secret_key, load_settings
from .db import Database
from .slides import Catalog, SlidePool
from .storage import StorageUnavailable, create_storage
from .tilecache import TileCache

log = logging.getLogger(__name__)

WEB_DIR = BASE_DIR / "web"
TILE_CACHE_CONTROL = "private, max-age=604800"
THUMBNAIL_SIZE = (320, 320)
STAIN_LABELS = {"HE": "H&E"}

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "same-origin",  # ссылка на поле зрения не уходит на сторонние сайты
    "Content-Security-Policy": (
        "default-src 'self'; img-src 'self' data: blob:; style-src 'self' 'unsafe-inline'; "
        "frame-ancestors 'none'"
    ),
}


class LoginRequest(BaseModel):
    login: str
    password: str


def slide_title(row) -> str:
    if row["case_code"] is None:
        return f"Слайд {row['id'][:6].upper()}"
    stain = STAIN_LABELS.get(row["stain"].upper(), row["stain"])
    return f"{row['case_code']} · {row['glass']} · {stain}"


def slide_summary(row) -> dict:
    # Ключ (имя файла) клиенту не отдаётся: в именах могут оказаться персональные данные.
    return {
        "id": row["id"],
        "title": slide_title(row),
        "case": row["case_code"],
        "glass": row["glass"],
        "stain": row["stain"],
        "width": row["width"],
        "height": row["height"],
        "objective": row["objective"],
        "mpp": row["mpp"],
        "size_bytes": row["size"],
        "added_at": row["added_at"],
    }


def create_app() -> FastAPI:
    settings = load_settings()
    db = Database(settings.db_path)
    storage = create_storage(settings.storage)
    pool = SlidePool(storage, settings.tiles, settings.open_slides)
    catalog = Catalog(db, storage, pool, settings.sync_minutes)
    tile_cache = TileCache(settings.tile_cache_dir, int(settings.cache.max_gb * 1e9))
    throttle = LoginThrottle()
    settings.thumbs_dir.mkdir(parents=True, exist_ok=True)

    def initial_sync() -> None:
        try:
            log.info("Каталог синхронизирован: %s", catalog.sync())
        except StorageUnavailable as exc:
            log.warning("Хранилище недоступно при запуске: %s", exc)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        tile_cache.start()
        threading.Thread(target=initial_sync, name="catalog-sync", daemon=True).start()
        yield
        tile_cache.stop()
        pool.close_all()

    app = FastAPI(title="Вьювер гистосканов", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.db = db
    app.add_middleware(
        SessionMiddleware,
        secret_key=load_secret_key(settings),
        session_cookie="viewer_session",
        max_age=settings.auth.session_hours * 3600,
        same_site="lax",
        https_only=settings.auth.https_only,
    )

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
            content={"detail": "Хранилище слайдов временно недоступно. Попробуйте позже или сообщите администратору."},
        )

    app.mount("/static", StaticFiles(directory=WEB_DIR / "static"), name="static")

    # ---------- страницы ----------

    def page(name: str) -> FileResponse:
        return FileResponse(WEB_DIR / "pages" / name, headers={"Cache-Control": "no-cache"})

    def protected_page(request: Request, name: str):
        if session_user(request) is None:
            target = request.url.path + (f"?{request.url.query}" if request.url.query else "")
            return RedirectResponse(f"/login?next={quote(target, safe='')}", status_code=303)
        return page(name)

    @app.get("/login")
    def login_page():
        return page("login.html")

    @app.get("/")
    def catalog_page(request: Request):
        return protected_page(request, "index.html")

    @app.get("/viewer")
    def viewer_page(request: Request):
        return protected_page(request, "viewer.html")

    # ---------- вход ----------

    @app.post("/api/login")
    def login(body: LoginRequest, request: Request):
        key = f"{body.login.lower()}|{request.client.host if request.client else ''}"
        if throttle.blocked(key):
            raise HTTPException(429, "Слишком много неудачных попыток. Повторите через несколько минут.")
        user = authenticate(db, body.login.strip(), body.password)
        if user is None:
            throttle.record_failure(key)
            raise HTTPException(401, "Неверный логин или пароль")
        throttle.reset(key)
        request.session.clear()
        request.session["user_id"] = user["id"]
        return {"login": user["login"], "role": user["role"], "name": user["name"]}

    @app.post("/api/logout")
    def logout(request: Request):
        request.session.clear()
        return {"ok": True}

    @app.get("/api/me")
    def me(user=Depends(require_user)):
        return {"login": user["login"], "role": user["role"], "name": user["name"]}

    # ---------- каталог ----------

    @app.get("/api/slides")
    def list_slides(user=Depends(require_user)):
        catalog.sync_if_stale()
        return [slide_summary(row) for row in catalog.list()]

    @app.post("/api/slides/rescan")
    def rescan(user=Depends(require_admin)):
        return catalog.sync()

    def get_slide_row(slide_id: str):
        row = catalog.get(slide_id)
        if row is None:
            raise HTTPException(404, "Слайд не найден")
        return row

    @app.get("/api/slides/{slide_id}")
    def slide_info(slide_id: str, user=Depends(require_user)):
        row = get_slide_row(slide_id)
        db.execute("INSERT INTO view_log (user_id, slide_id) VALUES (?, ?)", (user["id"], slide_id))
        info = slide_summary(row)
        info["tiles"] = {
            "url": f"/api/slides/{slide_id}/tiles/",
            "tile_size": settings.tiles.tile_size,
            "overlap": settings.tiles.overlap,
            "format": "jpg",
        }
        info["siblings"] = [{"id": s["id"], "title": slide_title(s)} for s in catalog.siblings(row)]
        return info

    # ---------- изображения ----------

    @app.get("/api/slides/{slide_id}/tiles/{level:int}/{col:int}_{row:int}.jpg")
    def tile(slide_id: str, level: int, col: int, row: int, user=Depends(require_user)):
        slide_row = get_slide_row(slide_id)
        cfg = settings.tiles
        namespace = f"{slide_id}-{int(slide_row['mtime'])}-{slide_row['size']}-{cfg.tile_size}-{cfg.overlap}-{cfg.jpeg_quality}"
        path = tile_cache.path(namespace, level, col, row)
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
        slide_row = get_slide_row(slide_id)
        path: Path = settings.thumbs_dir / f"{slide_id}-{int(slide_row['mtime'])}.jpg"
        if not path.exists():
            with pool.acquire(slide_row["key"]) as handle:
                try:
                    image = handle.slide.get_thumbnail(THUMBNAIL_SIZE)
                except Exception as exc:
                    raise StorageUnavailable(f"Ошибка чтения миниатюры: {exc}") from exc
            tmp = path.with_suffix(f".{threading.get_ident()}.tmp")
            image.convert("RGB").save(tmp, "JPEG", quality=85)
            tmp.replace(path)
        return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})

    return app


app = create_app()
