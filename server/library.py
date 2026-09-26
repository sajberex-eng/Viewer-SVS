"""Папки со сканами настольной программы (docs/TOR-desktop.md, раздел 6).

Пользователь подключает папки на своём компьютере; сканы открываются на месте,
без копирования. Каталог повторяет диск: у каждой папки каталога есть
`source_path` — папка на диске. Ключ скана — полный путь к файлу; он живёт
только в базе, в API уходят id и название.

Сверка с диском (`sync`) идёт при запуске, при открытии папки в каталоге и по
кнопке «Обновить» (НП-14):

- новый файл открывается, чтобы прочитать размеры и увеличение, и попадает в
  каталог; миниатюра строится в фоне (НП-16);
- файл, который не открылся, не пропадает молча: он виден с причиной (НП-17);
- пропавший файл помечается «не найден», его сведения остаются;
- переименованный или перенесённый файл узнаётся по отпечатку — размер и
  контрольная сумма первого и последнего мегабайта (НП-15): аннотации,
  окраска и расчёты остаются при нём.

Файлы на диске программа не меняет никогда (НП-13): здесь их только читают.
"""
from __future__ import annotations

import hashlib
import logging
import os
import threading
import time
from pathlib import Path

from . import stains
from .catalog import CatalogError, new_slide_id, parse_name
from .db import Database
from .slides import SlidePool, read_metadata
from .storage import StorageUnavailable, open_slide_file, slide_extension

log = logging.getLogger(__name__)

FINGERPRINT_CHUNK = 1 << 20   # первый и последний мегабайт
RESYNC_AFTER_S = 10.0         # открытие папки в каталоге не сверяет чаще
# Отключённые папки: их сканы ждут здесь повторного подключения со всеми
# аннотациями и расчётами. Папка служебная, в каталоге не показывается.
DETACHED = ":detached:"
SKIP_DIRS = {"$recycle.bin", "system volume information"}


def fingerprint(path: Path, size: int) -> str:
    """Отпечаток файла: размер и сумма первого и последнего мегабайта. Читается
    2 МБ, а не весь скан в несколько гигабайт."""
    digest = hashlib.sha256(str(size).encode("ascii"))
    with open(path, "rb") as f:
        digest.update(f.read(FINGERPRINT_CHUNK))
        if size > FINGERPRINT_CHUNK:
            f.seek(max(size - FINGERPRINT_CHUNK, FINGERPRINT_CHUNK))
            digest.update(f.read(FINGERPRINT_CHUNK))
    return digest.hexdigest()


# Сообщения OpenSlide приходят по-английски; самые частые — понятным текстом
KNOWN_REASONS = {
    "Unsupported or missing image file": "Файл не распознан как скан: повреждён или формат не поддерживается",
}


def reason_text(exc: Exception) -> str:
    text = str(exc)
    prefix = "Не удалось открыть слайд: "
    text = text[len(prefix):] if text.startswith(prefix) else text
    for known, russian in KNOWN_REASONS.items():
        if known in text:
            return russian
    return text


class Library:
    def __init__(self, db: Database, pool: SlidePool, warmer=None):
        self._db = db
        self._pool = pool
        self._warmer = warmer
        self._lock = threading.Lock()
        self._synced_at: dict[int, float] = {}

    # ---------- подключённые папки ----------

    def sources(self) -> list:
        return self._db.query(
            "SELECT * FROM folders WHERE parent_id IS NULL AND source_path IS NOT NULL AND source_path != ?",
            (DETACHED,),
        )

    def is_source(self, folder_row) -> bool:
        return (folder_row is not None and folder_row["parent_id"] is None
                and folder_row["source_path"] not in (None, DETACHED))

    def add_source(self, path: str, user) -> int:
        try:
            base = Path(path.strip().strip('"')).expanduser().resolve()
        except (OSError, RuntimeError) as exc:
            raise CatalogError(f"Папка не найдена: {exc}") from exc
        if not base.is_dir():
            raise CatalogError("Папка не найдена")
        for row in self.sources():
            known = Path(row["source_path"])
            if base == known:
                raise CatalogError("Эта папка уже добавлена")
            if base.is_relative_to(known):
                raise CatalogError(f"Эта папка уже видна внутри добавленной «{row['name']}»")
            if known.is_relative_to(base):
                raise CatalogError(f"Внутри неё уже добавлена папка «{row['name']}» — сначала отключите её")
        folder_id = self._db.insert(
            "INSERT INTO folders (parent_id, name, access_mode, created_by, source_path) VALUES (NULL, ?, 'admins', ?, ?)",
            (self._root_name(base), user["id"] if user else None, str(base)),
        )
        self.sync(folder_id, force=True)
        return folder_id

    def _root_name(self, base: Path) -> str:
        """Имя в дереве — имя папки; две папки «Сканы» на разных дисках различаются
        приписанным местом (НП-12)."""
        taken = {row["name"].casefold() for row in self._db.query("SELECT name FROM folders WHERE parent_id IS NULL")}
        name = base.name or str(base)
        if name.casefold() not in taken:
            return name
        candidate = f"{name} ({base.parent})"
        number = 2
        while candidate.casefold() in taken:
            candidate = f"{name} ({base.parent}, {number})"
            number += 1
        return candidate

    def remove_source(self, folder_id: int) -> int:
        """Отключить папку: она уходит из каталога, файлы на диске не трогаются.
        Сканы с аннотациями и расчётами ждут в служебной папке: при повторном
        подключении они узнаются по отпечатку."""
        root = self._db.query_one("SELECT * FROM folders WHERE id = ?", (folder_id,))
        if not self.is_source(root):
            raise CatalogError("Отключить можно только добавленную папку верхнего уровня")
        with self._lock:
            ids = self._subtree(folder_id)
            detached = self._detached_folder()
            marks = ",".join("?" * len(ids))
            for row in self._db.query(f"SELECT key FROM slides WHERE folder_id IN ({marks})", tuple(ids)):
                self._pool.release_key(row["key"])
            with self._db.transaction() as conn:
                moved = conn.execute(
                    f"UPDATE slides SET folder_id = ?, missing = 1 WHERE folder_id IN ({marks})",
                    (detached, *ids),
                ).rowcount
                for fid in sorted(ids, key=lambda f: -self._depth(f)):
                    conn.execute("DELETE FROM folders WHERE id = ?", (fid,))
            self._synced_at.pop(folder_id, None)
        return moved

    def _detached_folder(self) -> int:
        row = self._db.query_one("SELECT id FROM folders WHERE source_path = ?", (DETACHED,))
        if row:
            return row["id"]
        return self._db.insert(
            "INSERT INTO folders (parent_id, name, access_mode, source_path) VALUES (NULL, ?, 'admins', ?)",
            (DETACHED, DETACHED),
        )

    def detached_folder_id(self) -> int | None:
        row = self._db.query_one("SELECT id FROM folders WHERE source_path = ?", (DETACHED,))
        return row["id"] if row else None

    def root_of(self, folder_id: int) -> int | None:
        """Подключённая папка, внутри которой лежит эта."""
        current = self._db.query_one("SELECT id, parent_id FROM folders WHERE id = ?", (folder_id,))
        while current is not None and current["parent_id"] is not None:
            current = self._db.query_one("SELECT id, parent_id FROM folders WHERE id = ?", (current["parent_id"],))
        return current["id"] if current else None

    # ---------- сверка с диском ----------

    def sync_all(self) -> dict:
        totals = {"added": 0, "relinked": 0, "missing": 0, "restored": 0, "problems": 0, "unavailable": 0}
        for row in self.sources():
            result = self.sync(row["id"], force=True)
            for name in totals:
                totals[name] += int(result.get(name, 0))
        return totals

    def sync_folder(self, folder_id: int) -> dict:
        """Сверка при открытии папки в каталоге: всей подключённой папки, но не чаще
        раза в RESYNC_AFTER_S — щелчки по дереву не должны каждый раз обходить диск."""
        root = self.root_of(folder_id)
        if root is None or not self.is_source(self._db.query_one("SELECT * FROM folders WHERE id = ?", (root,))):
            raise CatalogError("Папка не найдена")
        return self.sync(root)

    def sync(self, root_id: int, force: bool = False) -> dict:
        with self._lock:
            if not force and time.monotonic() - self._synced_at.get(root_id, 0) < RESYNC_AFTER_S:
                return {"skipped": True}
            result = self._sync(root_id)
            self._synced_at[root_id] = time.monotonic()
            return result

    def _sync(self, root_id: int) -> dict:
        root = self._db.query_one("SELECT * FROM folders WHERE id = ?", (root_id,))
        base = Path(root["source_path"])
        stats = {"added": 0, "relinked": 0, "missing": 0, "restored": 0, "problems": 0}
        subtree = self._subtree(root_id)
        marks = ",".join("?" * len(subtree))
        known = {row["key"]: row for row in self._db.query(
            f"SELECT * FROM slides WHERE folder_id IN ({marks})", tuple(subtree))}

        if not base.is_dir():
            # Съёмный или сетевой диск не подключён: сканы помечаются «не найден»,
            # всё остальное остаётся как было
            for key, row in known.items():
                if not row["missing"]:
                    self._mark_missing(row)
                    stats["missing"] += 1
            return {**stats, "unavailable": 1}

        files, walk_errors = self._walk(base)
        seen: set[str] = set()
        problems: list[tuple[Path, str, str]] = []  # папка, имя файла, причина
        folder_ids: dict[Path, int] = {base: root_id}
        warm: list[str] = []

        for path in files:
            key = str(path)
            try:
                stat = path.stat()
            except OSError as exc:
                problems.append((path.parent, path.name, reason_text(exc)))
                continue
            folder_id = self._ensure_folder(path.parent, base, folder_ids)
            row = known.get(key) or self._db.query_one("SELECT * FROM slides WHERE key = ?", (key,))
            if row is not None and row["size"] == stat.st_size and row["mtime"] == stat.st_mtime:
                if row["missing"] or row["folder_id"] != folder_id:
                    self._db.execute("UPDATE slides SET missing = 0, folder_id = ? WHERE id = ?", (folder_id, row["id"]))
                    stats["restored"] += int(row["missing"])
                seen.add(key)
                continue
            try:
                print_ = fingerprint(path, stat.st_size)
            except OSError as exc:
                problems.append((path.parent, path.name, reason_text(exc)))
                continue
            if row is None:
                row = self._relink_candidate(print_)
                if row is not None:
                    self._pool.release_key(row["key"])
                    stats["relinked"] += 1
            try:
                meta = self._read(path)
            except StorageUnavailable as exc:
                problems.append((path.parent, path.name, reason_text(exc)))
                continue
            meta.update(size=stat.st_size, mtime=stat.st_mtime)
            if row is None:
                slide_id = self._insert(key, folder_id, path.name, print_, meta)
                stats["added"] += 1
            else:
                slide_id = row["id"]
                self._pool.release_key(row["key"])  # файл заменили: открытый держит прежний
                self._db.execute(
                    """UPDATE slides SET key = ?, folder_id = ?, original_name = ?, fingerprint = ?, missing = 0,
                              size = ?, mtime = ?, width = ?, height = ?, objective = ?, mpp = ?, has_label = ?
                       WHERE id = ?""",
                    (key, folder_id, path.name, print_, meta["size"], meta["mtime"], meta["width"], meta["height"],
                     meta["objective"], meta["mpp"], meta["has_label"], slide_id),
                )
            seen.add(key)
            warm.append(slide_id)

        for key, row in known.items():
            if key not in seen and not row["missing"]:
                # Файл мог уйти в другую подключённую папку — тогда его ключ уже сменился
                current = self._db.query_one("SELECT key, missing FROM slides WHERE id = ?", (row["id"],))
                if current and current["key"] == key:
                    self._mark_missing(row)
                    stats["missing"] += 1

        problems += [(folder, "", error) for folder, error in walk_errors]
        for folder, _name, _reason in problems:
            self._ensure_folder(folder, base, folder_ids)
        self._store_problems(subtree, problems, folder_ids)
        stats["problems"] = len(problems)
        self._prune_folders(root_id, set(folder_ids.values()))

        if self._warmer is not None:
            for slide_id in warm:
                self._warmer.enqueue(self._db.query_one("SELECT * FROM slides WHERE id = ?", (slide_id,)))
        log.info("Папка %s сверена с диском: %s", root_id, stats)
        return stats

    def _walk(self, base: Path) -> tuple[list[Path], list[tuple[Path, str]]]:
        files: list[Path] = []
        errors: list[tuple[Path, str]] = []

        def failed(error: OSError) -> None:
            if error.filename and Path(error.filename) != base:
                errors.append((Path(error.filename), f"Папка не читается: {error.strerror or error}"))

        for folder, dirs, names in os.walk(base, onerror=failed):
            dirs[:] = sorted(d for d in dirs if not d.startswith(".") and d.casefold() not in SKIP_DIRS)
            for name in sorted(names):
                if slide_extension(name) and not name.startswith("~$"):
                    files.append(Path(folder) / name)
        return files, errors

    def _read(self, path: Path) -> dict:
        with open_slide_file(path) as slide:
            return read_metadata(slide)

    def _relink_candidate(self, print_: str):
        """Тот же файл под другим именем или в другой папке: запись, чей файл пропал."""
        for row in self._db.query("SELECT * FROM slides WHERE fingerprint = ? ORDER BY missing DESC", (print_,)):
            if row["missing"] or not Path(row["key"]).exists():
                return row
        return None  # такого файла ещё не было — или это его копия, она станет отдельным сканом

    def _insert(self, key: str, folder_id: int, name: str, print_: str, meta: dict) -> str:
        case_code, glass, stain_text = parse_name(name)
        slide_id = new_slide_id()
        self._db.execute(
            """
            INSERT INTO slides (id, key, folder_id, title, original_name, size, mtime, width, height,
                                objective, mpp, has_label, case_code, glass, stain, fingerprint)
            VALUES (?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (slide_id, key, folder_id, name, meta["size"], meta["mtime"], meta["width"], meta["height"],
             meta["objective"], meta["mpp"], meta["has_label"], case_code, glass,
             stains.from_file_name(stain_text) or stains.from_free_name(name), print_),
        )
        return slide_id

    def _mark_missing(self, row) -> None:
        self._pool.release_key(row["key"])
        self._db.execute("UPDATE slides SET missing = 1 WHERE id = ?", (row["id"],))

    def _ensure_folder(self, folder: Path, base: Path, cache: dict[Path, int]) -> int:
        if folder in cache:
            return cache[folder]
        if not folder.is_relative_to(base):  # не бывает, но цикл вверх по диску не должен быть бесконечным
            return cache[base]
        parent_id = self._ensure_folder(folder.parent, base, cache)
        row = self._db.query_one("SELECT id, parent_id, name FROM folders WHERE source_path = ?", (str(folder),))
        if row is None:
            folder_id = self._db.insert(
                "INSERT INTO folders (parent_id, name, source_path) VALUES (?, ?, ?)",
                (parent_id, folder.name, str(folder)),
            )
        else:
            folder_id = row["id"]
            if row["parent_id"] != parent_id or row["name"] != folder.name:
                self._db.execute("UPDATE folders SET parent_id = ?, name = ? WHERE id = ?",
                                 (parent_id, folder.name, folder_id))
        cache[folder] = folder_id
        return folder_id

    def _store_problems(self, subtree: set[int], problems, folder_ids: dict[Path, int]) -> None:
        ids = subtree | set(folder_ids.values())
        marks = ",".join("?" * len(ids))
        with self._db.transaction() as conn:
            conn.execute(f"DELETE FROM scan_problems WHERE folder_id IN ({marks})", tuple(ids))
            conn.executemany(
                "INSERT INTO scan_problems (folder_id, name, reason) VALUES (?, ?, ?)",
                [(folder_ids[folder], name, reason) for folder, name, reason in problems],
            )

    def _prune_folders(self, root_id: int, needed: set[int]) -> None:
        """Папка остаётся, пока в ней (на любой глубине) есть сканы — пусть и
        пропавшие — или файлы с ошибкой; пустые папки диска в каталог не идут."""
        keep = set(needed)
        for row in self._db.query("SELECT DISTINCT folder_id FROM slides"):
            keep.add(row["folder_id"])
        parents = {row["id"]: row["parent_id"] for row in self._db.query("SELECT id, parent_id FROM folders")}
        for folder_id in list(keep):
            while folder_id is not None and folder_id in parents:
                keep.add(folder_id)
                folder_id = parents[folder_id]
        for folder_id in sorted(self._subtree(root_id), key=lambda f: -self._depth(f)):
            if folder_id != root_id and folder_id not in keep:
                self._db.execute("DELETE FROM folders WHERE id = ?", (folder_id,))

    def _subtree(self, folder_id: int) -> set[int]:
        children: dict[int, list[int]] = {}
        for row in self._db.query("SELECT id, parent_id FROM folders WHERE parent_id IS NOT NULL"):
            children.setdefault(row["parent_id"], []).append(row["id"])
        result, stack = {folder_id}, [folder_id]
        while stack:
            for kid in children.get(stack.pop(), []):
                result.add(kid)
                stack.append(kid)
        return result

    def _depth(self, folder_id: int) -> int:
        depth, current = 0, self._db.query_one("SELECT parent_id FROM folders WHERE id = ?", (folder_id,))
        while current is not None and current["parent_id"] is not None:
            depth += 1
            current = self._db.query_one("SELECT parent_id FROM folders WHERE id = ?", (current["parent_id"],))
        return depth

    # ---------- для каталога ----------

    def problems(self) -> list[dict]:
        return [
            {"folder_id": row["folder_id"], "name": row["name"], "reason": row["reason"]}
            for row in self._db.query("SELECT * FROM scan_problems ORDER BY folder_id, name")
        ]
