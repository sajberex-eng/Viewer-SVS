"""SQLite-каталог: папки, слайды, пользователи, права доступа, журнал.

Схема версионируется через PRAGMA user_version:
  1  дерево папок и права вместо плоского списка слайдов (база редакции 2
     переносится автоматически);
  2  незавершённые загрузки;
  3  группы пользователей; обязательной смены выданного пароля больше нет;
  4  аннотации на сканах (этап 9);
  5  цвет аннотации;
  6  окраска кодом из списка и маркер ИГХ отдельно (этап 11, ОК-3).
"""
from __future__ import annotations

import sqlite3
import threading
from contextlib import closing, contextmanager
from pathlib import Path

from . import stains

SCHEMA_VERSION = 6

# Группы, которые заводятся при создании базы. Дальше администратор
# сам создаёт, переименовывает и удаляет их. «Администраторы» в таблице
# не хранятся: это роль admin, у которой доступ есть всегда.
DEFAULT_GROUPS = ("Патологи", "Гематологи", "Резиденты")


def utc_iso(value: str | None) -> str | None:
    """Время из базы для API: CURRENT_TIMESTAMP пишет UTC без пояса
    («2026-09-19 14:42:00»), наружу оно уходит с явной отметкой «Z»,
    чтобы страница показала его по часам пользователя (ИН-1)."""
    return f"{value.replace(' ', 'T')}Z" if value else value

# Папка для слайдов из базы редакции 2, где прав доступа ещё не было.
# Режим «только администраторы»: пока админ не решит иначе, их никто не видит.
LEGACY_FOLDER_NAME = "Загружено ранее"

SCHEMA = """
CREATE TABLE users (
    id                   INTEGER PRIMARY KEY,
    login                TEXT NOT NULL UNIQUE,
    password_hash        TEXT NOT NULL,
    role                 TEXT NOT NULL CHECK (role IN ('user', 'admin')),
    name                 TEXT NOT NULL DEFAULT '',
    status               TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'blocked')),
    expires_at           TEXT,
    -- растёт при блокировке, смене и сбросе пароля: старые сессии сразу перестают действовать
    session_epoch        INTEGER NOT NULL DEFAULT 0,
    last_login_at        TEXT,
    created_at           TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE folders (
    id          INTEGER PRIMARY KEY,
    parent_id   INTEGER REFERENCES folders(id) ON DELETE RESTRICT,
    name        TEXT NOT NULL,
    -- NULL означает «наследовать от папки выше»; у корневой наследовать не от кого
    access_mode TEXT CHECK (access_mode IN ('admins', 'all', 'selected')),
    created_by  INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (parent_id IS NOT NULL OR access_mode IS NOT NULL)
);

-- В SQLite NULL != NULL, поэтому для корня нужен отдельный индекс
CREATE UNIQUE INDEX folders_name_in_parent ON folders(parent_id, name) WHERE parent_id IS NOT NULL;
CREATE UNIQUE INDEX folders_name_at_root   ON folders(name)            WHERE parent_id IS NULL;

CREATE TABLE slides (
    id            TEXT PRIMARY KEY,
    key           TEXT NOT NULL UNIQUE,      -- ключ в хранилище, клиенту не отдаётся
    folder_id     INTEGER NOT NULL REFERENCES folders(id) ON DELETE RESTRICT,
    title         TEXT,                      -- название в каталоге, задаёт администратор
    original_name TEXT,                      -- исходное имя файла, видно только администратору
    note          TEXT NOT NULL DEFAULT '',
    access_mode   TEXT CHECK (access_mode IN ('admins', 'all', 'selected')),
    size          INTEGER NOT NULL,
    mtime         REAL NOT NULL,
    width         INTEGER NOT NULL,
    height        INTEGER NOT NULL,
    objective     REAL,
    mpp           REAL,
    has_label     INTEGER NOT NULL DEFAULT 0,
    case_code     TEXT,
    glass         TEXT,
    stain         TEXT,                      -- код из stains.CODES (версия 6)
    ihc_marker    TEXT,                      -- маркер ИГХ, только при stain = 'IHC'
    missing       INTEGER NOT NULL DEFAULT 0,
    uploaded_by   INTEGER REFERENCES users(id) ON DELETE SET NULL,
    added_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX slides_folder ON slides(folder_id);

-- Разрешения для режима «выбранные пользователи»: ровно на папку или на слайд
CREATE TABLE access_grants (
    id        INTEGER PRIMARY KEY,
    user_id   INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    folder_id INTEGER REFERENCES folders(id) ON DELETE CASCADE,
    slide_id  TEXT REFERENCES slides(id) ON DELETE CASCADE,
    granted_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
    granted_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK ((folder_id IS NULL) <> (slide_id IS NULL))
);

CREATE UNIQUE INDEX access_grants_folder ON access_grants(user_id, folder_id) WHERE folder_id IS NOT NULL;
CREATE UNIQUE INDEX access_grants_slide  ON access_grants(user_id, slide_id)  WHERE slide_id IS NOT NULL;

-- Журнал переживает удаление пользователя: логин хранится текстом (ТЗ: не менее года)
CREATE TABLE audit_log (
    id          INTEGER PRIMARY KEY,
    at          TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    user_id     INTEGER REFERENCES users(id) ON DELETE SET NULL,
    actor       TEXT NOT NULL DEFAULT '',
    action      TEXT NOT NULL,
    object_type TEXT,
    object_id   TEXT,
    detail      TEXT,
    ip          TEXT
);

CREATE INDEX audit_log_at ON audit_log(at);
"""

# Версия 2: незавершённые загрузки. Файл принимается частями и дописывается
# в конец, поэтому после обрыва связи докачка идёт с последней принятой части.
UPLOADS_SCHEMA = """
CREATE TABLE uploads (
    id            TEXT PRIMARY KEY,
    folder_id     INTEGER NOT NULL REFERENCES folders(id) ON DELETE CASCADE,
    original_name TEXT NOT NULL,
    size          INTEGER NOT NULL,
    received      INTEGER NOT NULL DEFAULT 0,
    user_id       INTEGER REFERENCES users(id) ON DELETE CASCADE,
    started_at    TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at    TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""

# Версия 3: группы. Разрешения групп лежат отдельно от разрешений пользователей,
# форма у них одинаковая: «на папку» либо «на скан».
GROUPS_SCHEMA = """
CREATE TABLE user_groups (
    id         INTEGER PRIMARY KEY,
    name       TEXT NOT NULL UNIQUE COLLATE NOCASE,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE user_group_members (
    group_id INTEGER NOT NULL REFERENCES user_groups(id) ON DELETE CASCADE,
    user_id  INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    PRIMARY KEY (group_id, user_id)
);

CREATE INDEX user_group_members_user ON user_group_members(user_id);

CREATE TABLE group_grants (
    id         INTEGER PRIMARY KEY,
    group_id   INTEGER NOT NULL REFERENCES user_groups(id) ON DELETE CASCADE,
    folder_id  INTEGER REFERENCES folders(id) ON DELETE CASCADE,
    slide_id   TEXT REFERENCES slides(id) ON DELETE CASCADE,
    granted_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
    granted_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK ((folder_id IS NULL) <> (slide_id IS NULL))
);

CREATE UNIQUE INDEX group_grants_folder ON group_grants(group_id, folder_id) WHERE folder_id IS NOT NULL;
CREATE UNIQUE INDEX group_grants_slide  ON group_grants(group_id, slide_id)  WHERE slide_id IS NOT NULL;
"""

# Версия 4: аннотации (этап 9). Геометрия хранится в координатах скана, а не
# экрана: пометка держится за ту же клетку при любом увеличении и повороте.
# Логин автора продублирован текстом, как в журнале: аннотация переживает
# удаление учётной записи.
ANNOTATIONS_SCHEMA = """
CREATE TABLE annotations (
    id         TEXT PRIMARY KEY,
    slide_id   TEXT NOT NULL REFERENCES slides(id) ON DELETE CASCADE,
    kind       TEXT NOT NULL CHECK (kind IN ('point', 'polygon')),
    points     TEXT NOT NULL,            -- JSON: [[x, y], ...] в пикселях скана
    comment    TEXT NOT NULL DEFAULT '',
    color      TEXT NOT NULL DEFAULT 'green',  -- версия 5: 'green' или 'red', проверяет annotations.py
    author_id  INTEGER REFERENCES users(id) ON DELETE SET NULL,
    author     TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX annotations_slide ON annotations(slide_id);
"""

# Схема для чистой установки: всегда последняя версия
SCHEMA += UPLOADS_SCHEMA + GROUPS_SCHEMA + ANNOTATIONS_SCHEMA


def _seed_groups(conn: sqlite3.Connection) -> None:
    conn.executemany("INSERT INTO user_groups (name) VALUES (?)", [(name,) for name in DEFAULT_GROUPS])


class SchemaTooNew(RuntimeError):
    """База создана более новой версией сервиса."""


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def _migrate_v0_to_v1(conn: sqlite3.Connection) -> None:
    """База редакции 2: плоский список слайдов, роли резидент/преподаватель/администратор.

    Слайды складываются в папку «Загружено ранее» с доступом только для
    администраторов: прав раньше не было, и открывать их всем нельзя.
    """
    conn.executescript(
        """
        CREATE TABLE folders (
            id          INTEGER PRIMARY KEY,
            parent_id   INTEGER REFERENCES folders(id) ON DELETE RESTRICT,
            name        TEXT NOT NULL,
            access_mode TEXT CHECK (access_mode IN ('admins', 'all', 'selected')),
            created_by  INTEGER REFERENCES users(id) ON DELETE SET NULL,
            created_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CHECK (parent_id IS NOT NULL OR access_mode IS NOT NULL)
        );
        CREATE UNIQUE INDEX folders_name_in_parent ON folders(parent_id, name) WHERE parent_id IS NOT NULL;
        CREATE UNIQUE INDEX folders_name_at_root   ON folders(name)            WHERE parent_id IS NULL;

        CREATE TABLE users_v1 (
            id                   INTEGER PRIMARY KEY,
            login                TEXT NOT NULL UNIQUE,
            password_hash        TEXT NOT NULL,
            role                 TEXT NOT NULL CHECK (role IN ('user', 'admin')),
            name                 TEXT NOT NULL DEFAULT '',
            status               TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'blocked')),
            expires_at           TEXT,
            must_change_password INTEGER NOT NULL DEFAULT 0,
            session_epoch        INTEGER NOT NULL DEFAULT 0,
            last_login_at        TEXT,
            created_at           TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        INSERT INTO users_v1 (id, login, password_hash, role, name, created_at)
            SELECT id, login, password_hash,
                   CASE role WHEN 'admin' THEN 'admin' ELSE 'user' END,
                   name, created_at
            FROM users;
        DROP TABLE users;
        ALTER TABLE users_v1 RENAME TO users;
        """
    )

    # Папка нужна, только если переносить есть что: folder_id у слайда обязателен.
    if conn.execute("SELECT count(*) FROM slides").fetchone()[0]:
        folder_id = conn.execute(
            "INSERT INTO folders (parent_id, name, access_mode) VALUES (NULL, ?, 'admins')",
            (LEGACY_FOLDER_NAME,),
        ).lastrowid
    else:
        folder_id = "NULL"

    conn.executescript(
        f"""
        CREATE TABLE slides_v1 (
            id            TEXT PRIMARY KEY,
            key           TEXT NOT NULL UNIQUE,
            folder_id     INTEGER NOT NULL REFERENCES folders(id) ON DELETE RESTRICT,
            title         TEXT,
            original_name TEXT,
            note          TEXT NOT NULL DEFAULT '',
            access_mode   TEXT CHECK (access_mode IN ('admins', 'all', 'selected')),
            size          INTEGER NOT NULL,
            mtime         REAL NOT NULL,
            width         INTEGER NOT NULL,
            height        INTEGER NOT NULL,
            objective     REAL,
            mpp           REAL,
            has_label     INTEGER NOT NULL DEFAULT 0,
            case_code     TEXT,
            glass         TEXT,
            stain         TEXT,
            missing       INTEGER NOT NULL DEFAULT 0,
            uploaded_by   INTEGER REFERENCES users(id) ON DELETE SET NULL,
            added_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        INSERT INTO slides_v1 (id, key, folder_id, size, mtime, width, height,
                               objective, mpp, case_code, glass, stain, missing, added_at)
            SELECT id, key, {folder_id}, size, mtime, width, height,
                   objective, mpp, case_code, glass, stain, missing, added_at
            FROM slides;
        DROP TABLE slides;
        ALTER TABLE slides_v1 RENAME TO slides;
        CREATE INDEX slides_folder ON slides(folder_id);

        CREATE TABLE access_grants (
            id         INTEGER PRIMARY KEY,
            user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            folder_id  INTEGER REFERENCES folders(id) ON DELETE CASCADE,
            slide_id   TEXT REFERENCES slides(id) ON DELETE CASCADE,
            granted_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
            granted_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CHECK ((folder_id IS NULL) <> (slide_id IS NULL))
        );
        CREATE UNIQUE INDEX access_grants_folder ON access_grants(user_id, folder_id) WHERE folder_id IS NOT NULL;
        CREATE UNIQUE INDEX access_grants_slide  ON access_grants(user_id, slide_id)  WHERE slide_id IS NOT NULL;

        CREATE TABLE audit_log (
            id          INTEGER PRIMARY KEY,
            at          TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            user_id     INTEGER REFERENCES users(id) ON DELETE SET NULL,
            actor       TEXT NOT NULL DEFAULT '',
            action      TEXT NOT NULL,
            object_type TEXT,
            object_id   TEXT,
            detail      TEXT,
            ip          TEXT
        );
        CREATE INDEX audit_log_at ON audit_log(at);

        INSERT INTO audit_log (at, user_id, actor, action, object_type, object_id)
            SELECT v.opened_at, v.user_id, coalesce(u.login, ''), 'slide.open', 'slide', v.slide_id
            FROM view_log v LEFT JOIN users u ON u.id = v.user_id;
        DROP TABLE view_log;
        """
    )


def _migrate_stains(conn: sqlite3.Connection) -> None:
    """Версия 6: окраска была свободным текстом, стала кодом из списка (ОК-3).

    «HE» становится H&E, любой другой текст — маркером ИГХ: так до версии 6
    записывались только маркеры (Ki-67, CD3…).
    """
    conn.execute("ALTER TABLE slides ADD COLUMN ihc_marker TEXT")
    for row in conn.execute("SELECT id, stain FROM slides WHERE stain IS NOT NULL").fetchall():
        code, marker = stains.from_text(row[1])
        conn.execute("UPDATE slides SET stain = ?, ihc_marker = ? WHERE id = ?", (code, marker, row[0]))


class Database:
    """Подключение держится на поток (СК-3).

    Раньше соединение открывалось на каждый запрос. На проверке прав это давало
    пять открытий базы на каждый тайл — 5,8 мс, то есть больше, чем сама отдача
    готового тайла. SQLite не разрешает делить подключение между потоками,
    поэтому у каждого потока своё; потоки берутся из пула сервера и живут долго.
    """

    def __init__(self, path: Path):
        self.path = path
        self._local = threading.local()
        self._all: list[sqlite3.Connection] = []
        self._lock = threading.Lock()
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as conn:
            conn.execute("PRAGMA journal_mode = WAL")
            self._prepare(conn)

    def _prepare(self, conn: sqlite3.Connection) -> None:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            raise SchemaTooNew(
                f"База создана более новой версией сервиса (схема {version}, поддерживается {SCHEMA_VERSION})"
            )
        if version == SCHEMA_VERSION:
            return

        # Пересборка таблиц ломает внешние ключи, пока идёт перенос данных.
        conn.execute("PRAGMA foreign_keys = OFF")
        try:
            with conn:
                if not _tables(conn):
                    conn.executescript(SCHEMA)
                    _seed_groups(conn)
                else:
                    if version == 0:
                        _migrate_v0_to_v1(conn)
                        version = 1
                    if version == 1:
                        conn.executescript(UPLOADS_SCHEMA)
                        version = 2
                    if version == 2:
                        conn.executescript(GROUPS_SCHEMA)
                        _seed_groups(conn)
                        # Обязательной смены пароля больше нет (решение заказчика)
                        conn.execute("ALTER TABLE users DROP COLUMN must_change_password")
                        version = 3
                    if version == 3:
                        conn.executescript(ANNOTATIONS_SCHEMA)  # таблица сразу с цветом
                        version = 5
                    if version == 4:
                        # Цвет аннотации (решение заказчика 2026-09-20): прежние становятся зелёными
                        conn.execute("ALTER TABLE annotations ADD COLUMN color TEXT NOT NULL DEFAULT 'green'")
                        version = 5
                    if version == 5:
                        _migrate_stains(conn)
                        version = 6
                conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            broken = conn.execute("PRAGMA foreign_key_check").fetchall()
            if broken:
                raise RuntimeError(f"после переноса базы нарушены связи: {broken[:5]}")
        finally:
            conn.execute("PRAGMA foreign_keys = ON")

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def connection(self) -> sqlite3.Connection:
        """Подключение этого потока, открывается при первом обращении."""
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._local.conn = self._connect()
            with self._lock:
                self._all.append(conn)
        return conn

    def close_all(self) -> None:
        """Закрывает подключения при остановке сервиса и в тестах.

        Подключение чужого потока закрыть нельзя — SQLite это запрещает;
        такие остаются на совести интерпретатора при выходе.
        """
        with self._lock:
            connections, self._all = self._all, []
        self._local = threading.local()
        for conn in connections:
            try:
                conn.close()
            except sqlite3.Error:
                pass

    def query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        return self.connection().execute(sql, params).fetchall()

    def query_one(self, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def execute(self, sql: str, params: tuple = ()) -> int:
        conn = self.connection()
        with conn:
            return conn.execute(sql, params).rowcount

    def insert(self, sql: str, params: tuple = ()) -> int:
        """INSERT, возвращающий id новой строки."""
        conn = self.connection()
        with conn:
            return conn.execute(sql, params).lastrowid

    @contextmanager
    def transaction(self):
        """Несколько изменений одной транзакцией: либо все, либо ни одного."""
        conn = self.connection()
        with conn:
            yield conn
