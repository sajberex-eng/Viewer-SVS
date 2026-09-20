"""Учётные записи из командной строки: python -m server.manage <команда>.

Нужна для первого администратора: дальше пользователей заводит администратор
в веб-интерфейсе. Самостоятельной регистрации нет.
"""
from __future__ import annotations

import argparse
import getpass
import sqlite3
import sys

from .auth import ROLES, generate_password, hash_password, set_password
from .config import load_settings
from .db import Database
from .slides import SlidePool
from .storage import create_storage
from .tilecache import TileCache
from .warmup import Warmer


def read_password() -> str:
    password = getpass.getpass("Пароль: ")
    if password != getpass.getpass("Пароль ещё раз: "):
        sys.exit("Пароли не совпадают")
    return password


def warm_all(db: Database, settings) -> None:
    """Разовый прогрев уже загруженных сканов (СК-1).

    Новые сканы сервер прогревает сам сразу после приёма. Команду можно
    выполнять на работающем сервисе: файлы кэша пишутся через временное имя.
    """
    storage = create_storage(settings.storage)
    pool = SlidePool(storage, settings.tiles, settings.open_slides)
    cache = TileCache(settings.tile_cache_dir, int(settings.cache.max_gb * 1e9))
    settings.thumbs_dir.mkdir(parents=True, exist_ok=True)
    warmer = Warmer(pool, cache, settings.tiles, settings.thumbs_dir)

    rows = db.query("SELECT * FROM slides WHERE missing = 0 ORDER BY added_at")
    print(f"Сканов к прогреву: {len(rows)}")
    for number, row in enumerate(rows, 1):
        try:
            result = warmer.warm(row)
        except Exception as exc:  # один нечитаемый файл не должен останавливать остальные
            print(f"{number}/{len(rows)}  {row['id']}: не удалось — {exc}")
            continue
        print(f"{number}/{len(rows)}  {row['id']}: тайлов {result['tiles']}, за {result['seconds']:.1f} с")
    pool.close_all()
    print("Готово")


def main() -> None:
    parser = argparse.ArgumentParser(description="Учётные записи вьювера")
    commands = parser.add_subparsers(dest="command", required=True)

    add = commands.add_parser("add-user", help="создать пользователя")
    add.add_argument("login")
    add.add_argument("--role", choices=ROLES, default="user")
    add.add_argument("--name", default="", help="имя для отображения в интерфейсе")
    add.add_argument("--generate", action="store_true", help="сгенерировать пароль вместо ввода вручную")

    passwd = commands.add_parser("set-password", help="сменить пароль")
    passwd.add_argument("login")

    delete = commands.add_parser("del-user", help="удалить пользователя")
    delete.add_argument("login")

    commands.add_parser("list-users", help="показать пользователей")

    commands.add_parser(
        "warm",
        help="приготовить миниатюры и обзорные тайлы для сканов, загруженных до появления прогрева",
    )

    args = parser.parse_args()
    settings = load_settings()
    db = Database(settings.db_path)

    if args.command == "warm":
        warm_all(db, settings)
        return

    try:
        if args.command == "add-user":
            password = generate_password() if args.generate else read_password()
            db.execute(
                "INSERT INTO users (login, password_hash, role, name) VALUES (?, ?, ?, ?)",
                (args.login, hash_password(password), args.role, args.name or args.login),
            )
            print(f"Создан пользователь {args.login} ({args.role})")
            if args.generate:
                print(f"Пароль: {password}")
                print("Передайте его пользователю; сменить его можно в профиле.")
        elif args.command == "set-password":
            row = db.query_one("SELECT id FROM users WHERE login = ?", (args.login,))
            if row is None:
                sys.exit("Пользователь не найден")
            set_password(db, row["id"], read_password())
            print("Пароль изменён, прежние сессии завершены")
        elif args.command == "del-user":
            admins = db.query_one("SELECT count(*) AS n FROM users WHERE role = 'admin' AND status = 'active'")["n"]
            row = db.query_one("SELECT role FROM users WHERE login = ?", (args.login,))
            if row is None:
                sys.exit("Пользователь не найден")
            if row["role"] == "admin" and admins <= 1:
                sys.exit("Это последний администратор, удалять нельзя")
            db.execute("DELETE FROM users WHERE login = ?", (args.login,))
            print("Пользователь удалён")
        else:
            rows = db.query("SELECT login, role, name, status, expires_at, last_login_at FROM users ORDER BY login")
            for row in rows:
                expires = f"до {row['expires_at']}" if row["expires_at"] else "бессрочно"
                last = row["last_login_at"] or "не входил"
                print(f"{row['login']:<16} {row['role']:<6} {row['status']:<8} {expires:<14} {last:<20} {row['name']}")
            if not rows:
                print("Пользователей нет. Создайте администратора: add-user <логин> --role admin")
    except sqlite3.IntegrityError:
        sys.exit("Пользователь с таким логином уже есть")
    except ValueError as exc:
        sys.exit(str(exc))


if __name__ == "__main__":
    main()
