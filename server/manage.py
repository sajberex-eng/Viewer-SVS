"""Управление учётными записями: python -m server.manage <команда>.

Самостоятельной регистрации нет, пользователей создаёт администратор.
"""
from __future__ import annotations

import argparse
import getpass
import sqlite3
import sys

from .auth import ROLES, hash_password
from .config import load_settings
from .db import Database


def read_password() -> str:
    password = getpass.getpass("Пароль: ")
    if password != getpass.getpass("Пароль ещё раз: "):
        sys.exit("Пароли не совпадают")
    return password


def main() -> None:
    parser = argparse.ArgumentParser(description="Учётные записи вьювера")
    commands = parser.add_subparsers(dest="command", required=True)

    add = commands.add_parser("add-user", help="создать пользователя")
    add.add_argument("login")
    add.add_argument("--role", choices=ROLES, default="resident")
    add.add_argument("--name", default="", help="имя для отображения в интерфейсе")

    passwd = commands.add_parser("set-password", help="сменить пароль")
    passwd.add_argument("login")

    delete = commands.add_parser("del-user", help="удалить пользователя")
    delete.add_argument("login")

    commands.add_parser("list-users", help="показать пользователей")

    args = parser.parse_args()
    db = Database(load_settings().db_path)

    try:
        if args.command == "add-user":
            db.execute(
                "INSERT INTO users (login, password_hash, role, name) VALUES (?, ?, ?, ?)",
                (args.login, hash_password(read_password()), args.role, args.name or args.login),
            )
            print(f"Создан пользователь {args.login} ({args.role})")
        elif args.command == "set-password":
            if not db.execute(
                "UPDATE users SET password_hash = ? WHERE login = ?", (hash_password(read_password()), args.login)
            ):
                sys.exit("Пользователь не найден")
            print("Пароль изменён")
        elif args.command == "del-user":
            if not db.execute("DELETE FROM users WHERE login = ?", (args.login,)):
                sys.exit("Пользователь не найден")
            print("Пользователь удалён")
        else:
            for row in db.query("SELECT login, role, name, created_at FROM users ORDER BY login"):
                print(f"{row['login']:<20} {row['role']:<10} {row['name']:<30} {row['created_at']}")
    except sqlite3.IntegrityError:
        sys.exit("Пользователь с таким логином уже есть")
    except ValueError as exc:
        sys.exit(str(exc))


if __name__ == "__main__":
    main()
