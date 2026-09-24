"""Расчёты клеточности в сервисе: очередь, ход, хранение результата, маски тайлами
(этап 11, шаг 3: КЛ-3, КЛ-6…КЛ-9, КЛ-12…КЛ-14).

Контуры (КЛ-2) — аннотации видов «tissue» и «artifact» (annotations.py). При запуске
они снимаются в запись расчёта вместе с хэшем: если контуры потом поменяли, статус
показывает «устарел» (КЛ-8). Сам расчёт идёт в дочернем процессе (cellularity_job),
по одному за раз; здесь — поток-диспетчер, который берёт расчёты из очереди.

Маски (КЛ-6) хранятся картами классов по фрагментам (PNG с палитрой, индекс — часть)
в data/cellularity/<скан>/<расчёт>/ и отдаются тайлами той же сетки DeepZoom, что сам
скан, поэтому во вьювере ложатся слоем поверх препарата при любом повороте.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
import queue
import secrets
import shutil
import threading
from collections import OrderedDict
from pathlib import Path

import numpy as np
from PIL import Image

from . import annotations as annotationsvc
from . import audit, cellularity_job as job, cellularity_slide as cs
from .db import Database, utc_iso
from .tiles import DeepZoomGrid

log = logging.getLogger(__name__)

RESOLUTIONS = {"original": cs.ORIGINAL, "2": 2.0, "1": 1.0}
ACTIVE = ("queued", "running")
STAIN_FOR_TOOL = "HE"          # ОК-4: инструмент только у H&E
RESEARCH_NOTE = "Исследовательский показатель на стадии валидации, не диагноз"   # КЛ-13
MAPS_IN_MEMORY = 2             # сколько расчётов держать в памяти для тайлов масок
# Палитра карт классов: индекс → цвет (КЛ-6, цвета MarrowQuant); 0 — прозрачно
PALETTE = np.zeros((256, 4), np.uint8)
for _name, _index in cs.CLASS_INDEX.items():
    PALETTE[_index] = (*cs.CLASS_COLORS[_name], 255)


class CellularityError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def contours_hash(rows) -> str:
    """Хэш контуров скана: по нему видно, что после расчёта их меняли (КЛ-8)."""
    payload = json.dumps([[row["kind"], json.loads(row["points"])] for row in rows], ensure_ascii=False)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()


def fragments_from_rows(rows) -> list[cs.Fragment]:
    tissues = [[json.loads(row["points"])] for row in rows if row["kind"] == "tissue"]
    artifacts = [[json.loads(row["points"])] for row in rows if row["kind"] == "artifact"]
    return cs.fragments_from_polygons(tissues, artifacts)


class CellularityService:
    def __init__(self, db: Database, storage_config, data_dir: Path, pool, tiles_cfg, config=None):
        self.db = db
        self.storage_config = storage_config
        self.config = config
        self.root = Path(data_dir) / "cellularity"
        self.pool = pool
        self.tiles = tiles_cfg
        self._queue: queue.Queue[str] = queue.Queue()
        self._progress: dict[str, dict] = {}
        self._cancels: dict[str, threading.Event] = {}
        self._maps: OrderedDict[str, list] = OrderedDict()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ---------- жизненный цикл ----------

    def start(self) -> None:
        # Расчёты, оставшиеся от прошлого запуска сервиса, никто уже не ведёт
        self.db.execute(
            "UPDATE cellularity_runs SET status = 'failed', error = ?, finished_at = CURRENT_TIMESTAMP "
            "WHERE status IN ('queued', 'running')",
            ("сервис был перезапущен во время расчёта — запустите заново",),
        )
        self._thread = threading.Thread(target=self._loop, name="cellularity", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        for event in list(self._cancels.values()):
            event.set()
        self._queue.put("")

    # ---------- состояние для страницы ----------

    def contours(self, slide_id: str):
        return annotationsvc.contours(self.db, slide_id)

    def run_row(self, run_id: str):
        return self.db.query_one("SELECT * FROM cellularity_runs WHERE id = ?", (run_id,))

    def latest(self, slide_id: str):
        return self.db.query_one(
            "SELECT * FROM cellularity_runs WHERE slide_id = ? ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (slide_id,),
        )

    def status(self, slide_row, user) -> dict:
        rows = self.contours(slide_row["id"])
        latest = self.latest(slide_row["id"])
        return {
            "available": slide_row["stain"] == STAIN_FOR_TOOL and bool(slide_row["mpp"]),
            "can_run": annotationsvc.can_annotate(self.db, user),
            "contours": {
                "tissue": sum(1 for row in rows if row["kind"] == "tissue"),
                "artifact": sum(1 for row in rows if row["kind"] == "artifact"),
            },
            "resolutions": list(RESOLUTIONS),
            "resolution": self.default_resolution,
            "auto": bool(self.config and self.config.auto),
            "run": self.run_dict(latest, contours_hash(rows)) if latest else None,
            "note": RESEARCH_NOTE,
        }

    def run_dict(self, row, current_hash: str | None = None) -> dict:
        if current_hash is None:
            current_hash = contours_hash(self.contours(row["slide_id"]))
        out = {
            "id": row["id"],
            "slide_id": row["slide_id"],
            "status": row["status"],
            "resolution": row["resolution"],
            "pixel_um": row["pixel_um"],
            "algorithm": row["algorithm"],
            "started_by": row["started_by_name"],
            "created_at": utc_iso(row["created_at"]),
            "started_at": utc_iso(row["started_at"]) if row["started_at"] else None,
            "finished_at": utc_iso(row["finished_at"]) if row["finished_at"] else None,
            "error": row["error"],
            "peak_rss_mb": row["peak_rss_mb"],
            "elapsed_s": row["elapsed_s"],
            "stale": row["status"] == "done" and row["contours_hash"] != current_hash,
            "result": json.loads(row["result"]) if row["result"] else None,
            "progress": self._progress.get(row["id"]) if row["status"] in ACTIVE else None,
            "queue_ahead": self._queue_ahead(row) if row["status"] == "queued" else 0,
            "masks_url": f"/api/slides/{row['slide_id']}/cellularity/runs/{row['id']}/masks/"
            if row["status"] == "done" else None,
        }
        return out

    def _queue_ahead(self, row) -> int:
        return self.db.query_one(
            "SELECT count(*) AS n FROM cellularity_runs WHERE status = 'queued' AND created_at < ?",
            (row["created_at"],),
        )["n"]

    # ---------- запуск и отмена ----------

    @property
    def default_resolution(self) -> str:
        value = self.config.resolution if self.config else "2"
        return value if value in RESOLUTIONS else "2"

    def available(self, slide_row) -> bool:
        return slide_row["stain"] == STAIN_FOR_TOOL and bool(slide_row["mpp"])

    def start_run(self, slide_row, user, resolution: str | None = None, request=None,
                  propose: bool = False, auto: bool = False) -> dict:
        """Запуск расчёта. propose — если контуров нет, найти их по миниатюре (одна кнопка
        для патолога); auto — фоновый запуск после загрузки, помечается в журнале."""
        if slide_row["stain"] != STAIN_FOR_TOOL:
            raise CellularityError("Оценка клеточности есть только у сканов с окраской H&E")
        if not slide_row["mpp"]:
            raise CellularityError("В файле скана нет размера пикселя: площади посчитать нельзя")
        resolution = resolution or self.default_resolution
        if resolution not in RESOLUTIONS:
            raise CellularityError("Неизвестное разрешение расчёта")
        rows = self.contours(slide_row["id"])
        if not rows and propose:
            self.propose(slide_row, user, request)
            rows = self.contours(slide_row["id"])
        fragments = fragments_from_rows(rows)
        if not fragments:
            raise CellularityError("Фрагменты ткани не найдены: обведите их контуром «Ткань»")
        active = self.db.query_one(
            "SELECT id FROM cellularity_runs WHERE slide_id = ? AND status IN ('queued', 'running')",
            (slide_row["id"],),
        )
        if active:
            raise CellularityError("Расчёт по этому скану уже идёт", 409)
        spec = job.JobSpec(self.storage_config, slide_row["key"], float(slide_row["mpp"]),
                           RESOLUTIONS[resolution], fragments)
        try:
            job.check_memory(spec)
        except job.JobError as exc:
            raise CellularityError(str(exc)) from exc
        run_id = secrets.token_hex(6)   # случайный: два запуска подряд по одному скану не должны совпасть
        snapshot = json.dumps([{"kind": row["kind"], "points": json.loads(row["points"])} for row in rows],
                              ensure_ascii=False)
        self.db.execute(
            """
            INSERT INTO cellularity_runs (id, slide_id, status, resolution, contours, contours_hash,
                                          started_by, started_by_name)
            VALUES (?, ?, 'queued', ?, ?, ?, ?, ?)
            """,
            (run_id, slide_row["id"], resolution, snapshot, contours_hash(rows), user["id"], user["login"]),
        )
        audit.log(self.db, request, audit.CELLULARITY_START, user=user, object_type="slide",
                  object_id=slide_row["id"],
                  detail=f"расчёт {run_id}, разрешение {resolution}, фрагментов {len(fragments)}"
                  + (", автоматически после загрузки" if auto else ""))
        self._cancels[run_id] = threading.Event()
        self._queue.put(run_id)
        return self.run_dict(self.run_row(run_id), contours_hash(rows))

    def auto_run(self, slide_row, user) -> None:
        """Фоновый расчёт после загрузки или смены окраски на H&E (решение заказчика
        2026-09-24): контуры по миниатюре и расчёт при разрешении из настроек, чтобы
        патолог открыл скан с готовым результатом. Тихо пропускается, если инструмента
        у скана нет, расчёт уже идёт или свежий результат есть. Идёт в своём потоке:
        поиск контуров читает миниатюру, а ответ на загрузку ждать не должен."""
        if not (self.config and self.config.auto) or slide_row is None or not self.available(slide_row):
            return
        latest = self.latest(slide_row["id"])
        if latest is not None and (latest["status"] in ACTIVE or
                                   (latest["status"] == "done" and latest["contours_hash"] == contours_hash(self.contours(slide_row["id"])))):
            return

        def go():
            try:
                self.start_run(slide_row, user, propose=True, auto=True)
            except CellularityError as exc:
                log.info("Фоновый расчёт клеточности %s не запущен: %s", slide_row["id"], exc)
            except Exception:
                log.exception("Фоновый расчёт клеточности %s: ошибка запуска", slide_row["id"])

        threading.Thread(target=go, name="cellularity-auto", daemon=True).start()

    def cancel(self, row, user, request=None) -> dict:
        if row["status"] not in ACTIVE:
            raise CellularityError("Этот расчёт уже завершён")
        if row["status"] == "queued":
            self._finish(row["id"], "cancelled", error="отменён до начала")
        event = self._cancels.get(row["id"])
        if event is not None:
            event.set()
        audit.log(self.db, request, audit.CELLULARITY_CANCEL, user=user, object_type="slide",
                  object_id=row["slide_id"], detail=f"расчёт {row['id']}")
        return self.run_dict(self.run_row(row["id"]))

    def delete_run(self, row, user, request=None) -> None:
        """Удалить расчёт вместе с картами классов; идущий расчёт сначала отменяют."""
        if row["status"] in ACTIVE:
            raise CellularityError("Расчёт ещё идёт: сначала отмените его")
        shutil.rmtree(self.root / row["slide_id"] / row["id"], ignore_errors=True)
        with self._lock:
            self._maps.pop(row["id"], None)
        self.db.execute("DELETE FROM cellularity_runs WHERE id = ?", (row["id"],))
        audit.log(self.db, request, audit.CELLULARITY_DELETE, user=user, object_type="slide",
                  object_id=row["slide_id"], detail=f"расчёт {row['id']}")

    # ---------- диспетчер ----------

    def _loop(self) -> None:
        while not self._stop.is_set():
            run_id = self._queue.get()
            if not run_id or self._stop.is_set():
                continue
            try:
                self._execute(run_id)
            except Exception:  # диспетчер не должен умирать из-за одного расчёта
                log.exception("Расчёт клеточности %s: необработанная ошибка", run_id)
                self._finish(run_id, "failed", error="внутренняя ошибка сервиса")
            finally:
                self._progress.pop(run_id, None)
                self._cancels.pop(run_id, None)

    def _execute(self, run_id: str) -> None:
        row = self.run_row(run_id)
        if row is None or row["status"] != "queued":
            return
        slide = self.db.query_one("SELECT * FROM slides WHERE id = ?", (row["slide_id"],))
        if slide is None:
            self._finish(run_id, "failed", error="скан удалён")
            return
        contours = json.loads(row["contours"])
        fragments = cs.fragments_from_polygons(
            [[c["points"]] for c in contours if c["kind"] == "tissue"],
            [[c["points"]] for c in contours if c["kind"] == "artifact"],
        )
        masks_dir = self.root / slide["id"] / run_id
        spec = job.JobSpec(self.storage_config, slide["key"], float(slide["mpp"]),
                           RESOLUTIONS[row["resolution"]], fragments, masks_dir=masks_dir)
        self.db.execute("UPDATE cellularity_runs SET status = 'running', started_at = CURRENT_TIMESTAMP WHERE id = ?",
                        (run_id,))

        def on_progress(p: job.Progress) -> None:
            self._progress[run_id] = {"fragment": p.fragment, "of": p.of, "stage": p.stage,
                                      "fraction": round(p.fraction, 3), "rss_mb": round(p.rss_mb)}

        try:
            result = job.run_in_process(spec, on_progress=on_progress, cancel=self._cancels.get(run_id))
        except job.JobCancelled:
            shutil.rmtree(masks_dir, ignore_errors=True)
            self._finish(run_id, "cancelled", error="отменён")
            self._log_end(row, audit.CELLULARITY_CANCEL, "отменён")
            return
        except job.JobError as exc:
            shutil.rmtree(masks_dir, ignore_errors=True)
            self._finish(run_id, "failed", error=str(exc))
            self._log_end(row, audit.CELLULARITY_FAILED, str(exc))
            return
        total = result.get("total") or {}
        self.db.execute(
            """
            UPDATE cellularity_runs
               SET status = 'done', result = ?, pixel_um = ?, algorithm = ?, peak_rss_mb = ?, elapsed_s = ?,
                   finished_at = CURRENT_TIMESTAMP
             WHERE id = ?
            """,
            (json.dumps(result, ensure_ascii=False), result["pixel_um"], result["algorithm"],
             result.get("peak_rss_mb"), result.get("elapsed_s"), run_id),
        )
        self._log_end(row, audit.CELLULARITY_DONE,
                      f"клеточность {total.get('cellularity_eq1_pct')} % / {total.get('cellularity_eq2_pct')} %, "
                      f"{result.get('elapsed_s')} с, {result.get('peak_rss_mb')} МБ")

    def _finish(self, run_id: str, status: str, error: str | None = None) -> None:
        self.db.execute(
            "UPDATE cellularity_runs SET status = ?, error = ?, finished_at = CURRENT_TIMESTAMP WHERE id = ?",
            (status, error, run_id),
        )

    def _log_end(self, row, action: str, detail: str) -> None:
        audit.log(self.db, None, action, actor=row["started_by_name"], object_type="slide",
                  object_id=row["slide_id"], detail=f"расчёт {row['id']}: {detail}")

    # ---------- маски тайлами (КЛ-6) ----------

    def _maps_for(self, row) -> list:
        """Карты классов расчёта: (bbox, точек уровня 0 на точку карты, массив)."""
        with self._lock:
            cached = self._maps.get(row["id"])
            if cached is not None:
                self._maps.move_to_end(row["id"])
                return cached
        result = json.loads(row["result"])
        maps = []
        for item in result["fragments"]:
            path = self.root / row["slide_id"] / row["id"] / f"fragment_{item['fragment']}.png"
            if not path.exists():
                continue
            array = cs.load_class_map(path)
            x0, y0, x1, y1 = item["bbox"]
            maps.append(((x0, y0, x1, y1), (x1 - x0) / array.shape[1], (y1 - y0) / array.shape[0], array))
        with self._lock:
            self._maps[row["id"]] = maps
            while len(self._maps) > MAPS_IN_MEMORY:
                self._maps.popitem(last=False)
        return maps

    def mask_tile(self, slide_row, row, level: int, col: int, tile_row: int) -> bytes:
        grid = DeepZoomGrid(slide_row["width"], slide_row["height"], self.tiles.tile_size, self.tiles.overlap)
        if not 0 <= level < grid.level_count:
            raise CellularityError("Нет такого уровня", 404)
        cols, rows = grid.tile_count(level)
        if not (0 <= col < cols and 0 <= tile_row < rows):
            raise CellularityError("Нет такого тайла", 404)
        left, top, right, bottom = grid.tile_box(level, col, tile_row)
        scale = grid.scale(level)
        width, height = right - left, bottom - top
        index = np.zeros((height, width), np.uint8)
        xs = (left + np.arange(width) + 0.5) * scale      # центры точек тайла в координатах уровня 0
        ys = (top + np.arange(height) + 0.5) * scale
        for (x0, y0, x1, y1), fx, fy, array in self._maps_for(row):
            cx = np.floor((xs - x0) / fx).astype(np.int64)
            cy = np.floor((ys - y0) / fy).astype(np.int64)
            keep_x = (cx >= 0) & (cx < array.shape[1])
            keep_y = (cy >= 0) & (cy < array.shape[0])
            if not keep_x.any() or not keep_y.any():
                continue
            block = array[np.ix_(cy[keep_y], cx[keep_x])]
            target = index[np.ix_(keep_y, keep_x)]
            index[np.ix_(keep_y, keep_x)] = np.where(block != 0, block, target)
        image = Image.fromarray(PALETTE[index], "RGBA")
        buffer = io.BytesIO()
        image.save(buffer, "PNG", compress_level=3)
        return buffer.getvalue()

    # ---------- контуры по миниатюре (КЛ-2, желательное) ----------

    def propose(self, slide_row, user, request=None) -> list[dict]:
        if not slide_row["mpp"]:
            raise CellularityError("В файле скана нет размера пикселя")
        with self.pool.acquire(slide_row["key"]) as handle:
            fragments = cs.contours_auto(handle.slide, float(slide_row["mpp"]))
        created = []
        for fragment in fragments:
            points = cs.fragment_polygon(fragment)
            if len(points) < annotationsvc.MIN_POLYGON_POINTS:
                continue
            created.append(annotationsvc.create(self.db, slide_row["id"], "tissue", points,
                                                "предложен по миниатюре", user))
        audit.log(self.db, request, audit.ANNOTATION_CREATE, user=user, object_type="slide",
                  object_id=slide_row["id"], detail=f"tissue ×{len(created)}, контуры по миниатюре")
        return created

    def delete_contours(self, slide_row, user, request=None) -> dict:
        """Удалить все контуры скана, которые этому пользователю можно удалять:
        администратор — любые, патолог — свои. Чужие остаются, и их число возвращается."""
        rows = self.contours(slide_row["id"])
        removable = [row for row in rows if annotationsvc.can_edit(row, user)]
        for row in removable:
            annotationsvc.delete(self.db, row["id"])
        if removable:
            audit.log(self.db, request, audit.ANNOTATION_DELETE, user=user, object_type="slide",
                      object_id=slide_row["id"], detail=f"все контуры клеточности ×{len(removable)}")
        return {"deleted": len(removable), "kept": len(rows) - len(removable)}

    # ---------- выгрузка (КЛ-12) ----------

    CSV_FIELDS = ("run", "slide", "started_by", "finished_at", "resolution", "pixel_um", "algorithm", "fragment",
                  "tissue_mm2", "marrow_mm2", "bone_mm2", "hemato_mm2", "adip_mm2", "imv_mm2", "other_mm2",
                  "artifacts_mm2", "cellularity_eq1_pct", "cellularity_eq2_pct", "adiposity_pct", "imv_pct",
                  "other_pct", "adipocytes", "warnings")

    def export_csv(self) -> str:
        """Все выполненные расчёты: по фрагментам и итог по стеклу. Сканы — только по ID."""
        buffer = io.StringIO()
        writer = csv.writer(buffer, delimiter=";", lineterminator="\r\n")
        writer.writerow(self.CSV_FIELDS)
        rows = self.db.query("SELECT * FROM cellularity_runs WHERE status = 'done' ORDER BY finished_at, rowid")
        for row in rows:
            result = json.loads(row["result"])
            items = [*result["fragments"], {**result["total"], "fragment": "итог"}] if result.get("total") else result["fragments"]
            for item in items:
                writer.writerow([
                    row["id"], row["slide_id"], row["started_by_name"], utc_iso(row["finished_at"]),
                    row["resolution"], row["pixel_um"], row["algorithm"], item["fragment"],
                    *[item.get(key) for key in self.CSV_FIELDS[8:22]],
                    "; ".join(item.get("warnings") or []),
                ])
        writer.writerow([])
        writer.writerow([RESEARCH_NOTE])
        return "﻿" + buffer.getvalue()

    # ---------- уборка ----------

    def delete_slide_data(self, slide_id: str) -> None:
        shutil.rmtree(self.root / slide_id, ignore_errors=True)
        with self._lock:
            for run_id in [k for k, v in self._maps.items()]:
                self._maps.pop(run_id, None)
