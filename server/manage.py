"""Учётные записи и служебные команды: python -m server.manage <команда>.

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


def cellularity_cmd(db: Database, settings, args) -> None:
    """Оценка клеточности по MarrowQuant 2.0 (этап 11): расчёт без интерфейса, для сверки.

    Контуры — из GeoJSON, выгруженного из QuPath (классы «Tissue Boundaries» и
    «Artifact»), либо найденные по миниатюре. Результат — по фрагментам и итог.
    """
    import json
    from pathlib import Path

    from . import cellularity, cellularity_job as job, cellularity_slide as cs

    row = db.query_one("SELECT * FROM slides WHERE id = ?", (args.slide_id,))
    if row is None:
        sys.exit("Скан не найден")
    if not row["mpp"]:
        sys.exit("В файле скана нет размера пикселя: площади в мкм² посчитать нельзя")
    if row["stain"] != "HE":
        print("Внимание: окраска скана не H&E — пороги метода подобраны под гематоксилин и эозин")
    storage = create_storage(settings.storage)
    slide = storage.open_slide(row["key"])
    try:
        fragments = cs.contours_from_geojson(Path(args.contours)) if args.contours             else cs.contours_auto(slide, float(row["mpp"]))
        if not fragments:
            sys.exit("Фрагменты ткани не найдены")
        print(f"Скан {row['id']}: фрагментов {len(fragments)}, "
              f"контуры {'из файла' if args.contours else 'найдены по миниатюре'}")
        params = cellularity.Params()
        if args.adip_max is not None:
            params.adip_max_um2 = args.adip_max
        resolution = args.resolution if args.resolution == cs.ORIGINAL else float(args.resolution)
        masks_dir = Path(args.masks) if args.masks else None
        if args.in_process:
            result = cs.run(slide, float(row["mpp"]), resolution, fragments, params, masks_dir=masks_dir)
    finally:
        slide.close()
    if not args.in_process:
        # как в сервисе (КЛ-7): отдельный процесс с пониженным приоритетом, пределы памяти и времени
        spec = job.JobSpec(settings.storage, row["key"], float(row["mpp"]), resolution, fragments, params,
                           masks_dir=masks_dir, memory_limit_mb=args.memory_limit, time_limit_s=args.time_limit)

        def show(p: job.Progress) -> None:
            print(f"\r  фрагмент {p.fragment}/{p.of}: {p.stage:15s} {p.fraction * 100:3.0f} %", end="", flush=True)

        try:
            print(f"Оценка памяти: {job.check_memory(spec):.0f} МБ при пределе {args.memory_limit:.0f} МБ")
            result = job.run_in_process(spec, on_progress=show)
        except job.JobError as exc:
            print()
            sys.exit(f"Ошибка: {exc}")
        print(f"\r  готово: {result['elapsed_s']} с, пик памяти процесса {result['peak_rss_mb']} МБ".ljust(60))
        for item in result["fragments"]:
            print(f"фрагмент {item['fragment']}: {item['size_px'][0]}×{item['size_px'][1]}, ткани {item['tissue_mm2']:.2f} мм², "
                  f"клеточность {item['cellularity_eq1_pct']} % / {item['cellularity_eq2_pct']} %, {item['total_s']} с")
    total = result["total"]
    print(f"Итог по стеклу ({result['pixel_um']} мкм на точку): ткани {total['tissue_mm2']:.2f} мм², "
          f"костномозгового пространства {total['marrow_mm2']:.2f} мм²")
    print(f"  клеточность, формула 1 (Hm / (Hm + Ad)): {total['cellularity_eq1_pct']} %")
    print(f"  клеточность, формула 2 (Hm / пространство): {total['cellularity_eq2_pct']} %")
    print(f"  жир {total['adiposity_pct']} %, строма и сосуды {total['imv_pct']} %, прочее {total['other_pct']} %, "
          f"жировых клеток {total['adipocytes']}")
    for warning in total["warnings"]:
        print(f"  ! {warning}")
    print("Исследовательский показатель на стадии валидации, не диагноз.")
    if args.json:
        Path(args.json).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"Подробности: {args.json}")


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

    cell = commands.add_parser("cellularity", help="клеточность костного мозга по скану H&E (MarrowQuant 2.0)")
    cell.add_argument("slide_id")
    cell.add_argument("--resolution", default="original",
                      help="original (как MarrowQuant: уменьшение в 4 раза) или мкм на точку: 1, 2")
    cell.add_argument("--contours", help="GeoJSON из QuPath: классы Tissue Boundaries и Artifact")
    cell.add_argument("--adip-max", type=float, help="верхний предел площади жировой клетки, мкм² (в оригинале нет)")
    cell.add_argument("--masks", help="папка для карт частей (PNG, цвета MarrowQuant)")
    cell.add_argument("--json", help="файл для подробного результата")
    cell.add_argument("--memory-limit", type=float, default=500, help="предел памяти процесса расчёта, МБ (КЛ-7)")
    cell.add_argument("--time-limit", type=float, default=1800, help="предел времени расчёта, с")
    cell.add_argument("--in-process", action="store_true",
                      help="считать в этом процессе, без приоритета и пределов (для отладки)")

    args = parser.parse_args()
    settings = load_settings()
    db = Database(settings.db_path)

    if args.command == "warm":
        warm_all(db, settings)
        return
    if args.command == "cellularity":
        cellularity_cmd(db, settings, args)
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
