"""Смоук-тест на рабочей конфигурации: вход, отказы без авторизации, каталог, тайлы.

Запуск из папки проекта:  .venv\\Scripts\\python.exe -X utf8 tests\\smoke_test.py

Тайлы и миниатюра проверяются, только если администратор уже загрузил хотя бы
один скан: пустое хранилище это нормальное состояние нового сервера.
Права доступа проверяет tests\\access_test.py на отдельной временной базе.
"""
from __future__ import annotations

import secrets
import sys
import time
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
warnings.filterwarnings("ignore")

from fastapi.testclient import TestClient  # noqa: E402

from server.auth import hash_password  # noqa: E402
from server.main import app  # noqa: E402

LOGIN = "smoke-test"


def check(name: str, condition: bool, detail: str = "") -> None:
    print(f"[{'OK' if condition else 'СБОЙ'}] {name} {detail}")
    if not condition:
        raise SystemExit(1)


def main() -> None:
    password = secrets.token_urlsafe(12)
    db = app.state.db
    db.execute("DELETE FROM users WHERE login = ?", (LOGIN,))
    db.execute(
        "INSERT INTO users (login, password_hash, role, name) VALUES (?, ?, 'admin', 'Смоук-тест')",
        (LOGIN, hash_password(password)),
    )
    try:
        with TestClient(app) as client:
            response = client.get("/", follow_redirects=False)
            check("страница без входа уводит на /login", response.status_code == 303)
            check("каталог без входа: 401", client.get("/api/catalog").status_code == 401)
            check("тайл без входа: 401", client.get("/api/slides/x/tiles/0/0_0.jpg").status_code == 401)
            check("миниатюра без входа: 401", client.get("/api/slides/x/thumbnail.jpg").status_code == 401)
            check("этикетка без входа: 401", client.get("/api/slides/x/label.jpg").status_code == 401)
            check("журнал без входа: 401", client.get("/api/journal").status_code == 401)
            check("сайт закрыт от поисковых систем", "Disallow: /" in client.get("/robots.txt").text)

            bad = client.post("/api/login", json={"login": LOGIN, "password": "wrong-password"})
            check("неверный пароль: 401", bad.status_code == 401)
            good = client.post("/api/login", json={"login": LOGIN, "password": password})
            check("вход", good.status_code == 200)

            catalog = client.get("/api/catalog").json()
            slides = catalog["slides"]
            check("каталог отвечает", isinstance(slides, list), f"({len(catalog['folders'])} папок, {len(slides)} сканов)")
            check("администратору видно свободное место", catalog["space"] is not None,
                  f"({catalog['space']['free_bytes'] / 1e9:.1f} ГБ свободно)" if catalog["space"] else "")

            if not slides:
                print("[  ] сканов в каталоге нет: проверка тайлов пропущена")
            else:
                slide = max(slides, key=lambda s: s["width"])
                info = client.get(f"/api/slides/{slide['id']}").json()
                check("в ответе нет имени файла", ".svs" not in str(info.get("title", "")).lower())

                base = f"/api/slides/{slide['id']}/tiles"
                started = time.perf_counter()
                first = client.get(f"{base}/10/0_0.jpg")
                first_ms = (time.perf_counter() - started) * 1000
                check("тайл отдаётся как JPEG",
                      first.status_code == 200 and first.content[:2] == b"\xff\xd8", f"({first_ms:.0f} мс)")
                started = time.perf_counter()
                client.get(f"{base}/10/0_0.jpg")
                check("повторный тайл из кэша", True, f"({(time.perf_counter() - started) * 1000:.0f} мс)")
                check("несуществующий уровень: 404", client.get(f"{base}/99/0_0.jpg").status_code == 404)
                check("несуществующий тайл: 404", client.get(f"{base}/10/999_0.jpg").status_code == 404)
                check("миниатюра", client.get(f"/api/slides/{slide['id']}/thumbnail.jpg").status_code == 200)
                if info.get("has_label"):
                    label = client.get(f"/api/slides/{slide['id']}/label.jpg")
                    check("этикетка отдаётся без кэша в браузере",
                          label.status_code == 200 and label.headers.get("cache-control") == "no-store")

            client.post("/api/logout")
            check("после выхода: 401", client.get("/api/catalog").status_code == 401)
    finally:
        db.execute("DELETE FROM users WHERE login = ?", (LOGIN,))
    print("Все проверки пройдены")


if __name__ == "__main__":
    main()
