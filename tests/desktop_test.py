"""Настольный режим (docs/TOR-desktop.md, этап 1) на временной базе.

Запуск:  .venv\\Scripts\\python.exe -X utf8 tests\\desktop_test.py

Проверяет вход одноразовым ключом запуска (НП-9), отказ чужому Host (НП-10),
сессию до закрытия программы и обрыв сессий прошлого запуска (НП-11), отсутствие
входа и смены пароля (НП-2) и отметку data-desktop на страницах (НП-3, НП-4).
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

WORK_DIR = Path(tempfile.mkdtemp(prefix="viewer-desktop-test-"))
(WORK_DIR / "slides").mkdir()
(WORK_DIR / "config.yaml").write_text(
    f"storage:\n  root: {(WORK_DIR / 'slides').as_posix()!r}\n"
    f"data_dir: {(WORK_DIR / 'data').as_posix()!r}\n"
    "desktop: true\n",
    encoding="utf-8",
)
os.environ["VIEWER_CONFIG"] = str(WORK_DIR / "config.yaml")
os.environ["VIEWER_SECRET_KEY"] = "test-secret-key"

from fastapi.testclient import TestClient  # noqa: E402

from server.desktop import LOCAL_LOGIN, ensure_local_admin  # noqa: E402
from server.main import app  # noqa: E402

LOCAL = "http://127.0.0.1"
db = app.state.db
keys = app.state.launch_keys
failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    print(f"[{'OK' if condition else 'СБОЙ'}] {name} {detail}")
    if not condition:
        failures.append(name)


def enter(client: TestClient, key: str):
    return client.get(f"/desktop/enter?key={key}", follow_redirects=False)


def main() -> None:
    users = db.query("SELECT login, role FROM users")
    check("при запуске заведена одна учётная запись программы",
          [(u["login"], u["role"]) for u in users] == [(LOCAL_LOGIN, "admin")], str([dict(u) for u in users]))

    with TestClient(app, base_url=LOCAL) as window:
        response = window.get("/", follow_redirects=False)
        check("без сессии каталог уводит на /login", response.status_code == 303
              and response.headers["location"].startswith("/login"))
        page = window.get("/login")
        check("/login в программе — страница «запустите заново», без формы пароля",
              page.status_code == 200 and "desktop.closed.heading" in page.text and "password" not in page.text)
        check("страница помечена data-desktop", "<html data-desktop " in page.text)

        check("неверный ключ — отказ", enter(window, "wrong-key").status_code == 404)
        check("пустой ключ — отказ", enter(window, "").status_code == 404)

        key = keys.issue()
        response = enter(window, key)
        check("верный ключ открывает каталог", response.status_code == 303 and response.headers["location"] == "/")
        cookie = response.headers.get("set-cookie", "")
        check("cookie сессии живёт до закрытия программы (без Max-Age и Expires)",
              "viewer_session=" in cookie and "max-age" not in cookie.lower() and "expires" not in cookie.lower(), cookie)
        me = window.get("/api/me").json()
        check("вошёл администратор программы с отметкой desktop",
              me.get("login") == LOCAL_LOGIN and me.get("role") == "admin" and me.get("desktop") is True, str(me))
        check("каталог открывается", window.get("/").status_code == 200)
        check("права администратора: создание папки",
              window.post("/api/folders", json={"name": "Проверка"}).status_code == 200)

        other = TestClient(app, base_url=LOCAL)
        check("тот же ключ второй раз не действует", enter(other, key).status_code == 404)
        check("без сессии API отвечает 401", other.get("/api/me").status_code == 401)

        check("вход по паролю выключен", window.post(
            "/api/login", json={"login": LOCAL_LOGIN, "password": "anything-long"}).status_code == 404)
        check("смена пароля выключена", window.post(
            "/api/password", json={"current_password": "x" * 10, "new_password": "y" * 10}).status_code == 404)

        # Подмена DNS: чужой сайт, указывающий на 127.0.0.1, приходит со своим Host
        stranger = TestClient(app, base_url="http://attacker.example")
        check("запрос с чужим Host отклонён", stranger.get("/login").status_code == 400)
        check("ключ по чужому Host не принимается",
              enter(stranger, keys.issue()).status_code == 400)

        # Новый запуск программы: сессии прошлого запуска больше не действуют
        ensure_local_admin(db)
        app.state.perms.invalidate()
        check("после перезапуска старая сессия не действует", window.get("/api/me").status_code == 401)
        enter(window, keys.issue())
        check("новый ключ снова пускает", window.get("/api/me").status_code == 200)
        check("учётная запись по-прежнему одна",
              db.query_one("SELECT COUNT(*) AS n FROM users")["n"] == 1)


if __name__ == "__main__":
    try:
        main()
    finally:
        db.close_all()
        shutil.rmtree(WORK_DIR, ignore_errors=True)
    print(f"\nПроверок с ошибкой: {len(failures)}" if failures else "\nВсе проверки пройдены")
    sys.exit(1 if failures else 0)
