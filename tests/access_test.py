"""Проверка прав доступа на временной базе: рабочая база и хранилище не затрагиваются.

Запуск:  .venv\\Scripts\\python.exe -X utf8 tests\\access_test.py

Сканы для этого теста не нужны: записи каталога создаются прямо в базе, а право
проверяется раньше, чем сервер открывает файл. Поэтому «нет доступа» видно как
404, а пропущенный доступ — как любой другой ответ.
"""
from __future__ import annotations

import os
import shutil
import statistics
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

WORK_DIR = Path(tempfile.mkdtemp(prefix="viewer-access-test-"))
(WORK_DIR / "slides").mkdir()
(WORK_DIR / "config.yaml").write_text(
    f"storage:\n  root: {(WORK_DIR / 'slides').as_posix()!r}\n"
    f"data_dir: {(WORK_DIR / 'data').as_posix()!r}\n"
    "auth:\n  https_only: false\n",
    encoding="utf-8",
)
os.environ["VIEWER_CONFIG"] = str(WORK_DIR / "config.yaml")
os.environ["VIEWER_SECRET_KEY"] = "test-secret-key"

from fastapi.testclient import TestClient  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))  # synthetic_svs

from server.auth import hash_password  # noqa: E402
from server.main import app  # noqa: E402

db = app.state.db
failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    print(f"[{'OK' if condition else 'СБОЙ'}] {name} {detail}")
    if not condition:
        failures.append(name)


def make_user(login: str, role: str = "user") -> int:
    return db.insert(
        "INSERT INTO users (login, password_hash, role, name) VALUES (?, ?, ?, ?)",
        (login, hash_password("password-1234"), role, login),
    )


def make_slide(slide_id: str, folder_id: int, access_mode: str | None = None) -> None:
    db.execute(
        """
        INSERT INTO slides (id, key, folder_id, title, access_mode, size, mtime, width, height, has_label)
        VALUES (?, ?, ?, ?, ?, 1000, 0, 1000, 1000, 1)
        """,
        (slide_id, f"{slide_id[:2]}/{slide_id}.svs", folder_id, f"Скан {slide_id}", access_mode),
    )


def login(client: TestClient, user_login: str) -> None:
    response = client.post("/api/login", json={"login": user_login, "password": "password-1234"})
    assert response.status_code == 200, response.text


def catalog_ids(client: TestClient) -> tuple[set[int], set[str]]:
    data = client.get("/api/catalog").json()
    return {f["id"] for f in data["folders"]}, {s["id"] for s in data["slides"]}


def main() -> None:
    make_user("admin-test", "admin")
    make_user("user-a")
    make_user("user-b")
    user_a_id = db.query_one("SELECT id FROM users WHERE login = 'user-a'")["id"]
    user_b_id = db.query_one("SELECT id FROM users WHERE login = 'user-b'")["id"]

    # Жизненный цикл приложения запускается один раз; остальные клиенты нужны
    # только ради отдельных наборов cookie.
    with TestClient(app) as admin:
        alice, bob = TestClient(app), TestClient(app)
        login(admin, "admin-test")
        login(alice, "user-a")
        login(bob, "user-b")

        # ---------- папки и наследование ----------
        top = admin.post("/api/folders", json={"name": "2026-09-19"}).json()["id"]
        inner = admin.post("/api/folders", json={"name": "Случай 01", "parent_id": top}).json()["id"]
        make_slide("aaaaaaaaaaaa", top)
        make_slide("bbbbbbbbbbbb", inner)
        make_slide("cccccccccccc", inner)

        check("новая папка верхнего уровня закрыта: пользователь её не видит", catalog_ids(alice) == (set(), set()))
        check("администратор видит обе папки", catalog_ids(admin)[0] == {top, inner})

        admin.post("/api/access", json={"folder_id": top, "mode": "all"})
        folders, slides = catalog_ids(alice)
        check("режим «все»: видны папка и подпапка", folders == {top, inner}, f"({sorted(folders)})")
        check("режим «все»: видны все сканы", slides == {"aaaaaaaaaaaa", "bbbbbbbbbbbb", "cccccccccccc"})

        # Ключевое требование Д-2: свой режим заменяет унаследованный.
        # Скан закрывается отдельно, хотя папка выше открыта для всех.
        admin.post("/api/access", json={"slide_id": "bbbbbbbbbbbb", "mode": "admins"})
        check("закрытый скан не виден, хотя папка открыта", "bbbbbbbbbbbb" not in catalog_ids(alice)[1])

        admin.post("/api/access", json={"folder_id": inner, "mode": "selected", "user_ids": []})
        folders, slides = catalog_ids(alice)
        check("подпапка «выбранные» внутри «все» не видна остальным", inner not in folders, f"({sorted(folders)})")
        check("сканы закрытой подпапки не видны", {"bbbbbbbbbbbb", "cccccccccccc"}.isdisjoint(slides))
        check("скан в открытой папке остался виден", "aaaaaaaaaaaa" in slides)

        admin.post("/api/access", json={"folder_id": inner, "mode": "selected", "user_ids": [user_a_id]})
        folders, slides = catalog_ids(alice)
        check("после выдачи доступа подпапка видна", inner in folders, f"({sorted(folders)})")
        check("в ней виден скан без своего режима", "cccccccccccc" in slides)
        check("скан со своим режимом остаётся закрытым", "bbbbbbbbbbbb" not in slides)
        check("другому пользователю подпапка не видна", inner not in catalog_ids(bob)[0])

        # ---------- группы ----------
        listing = admin.get("/api/groups").json()
        by_name = {g["name"]: g["id"] for g in listing["groups"]}
        check("заведены группы Патологи, Гематологи, Резиденты",
              set(by_name) == {"Патологи", "Гематологи", "Резиденты"}, f"({sorted(by_name)})")
        check("«Администраторы» показана как встроенная роль", listing["administrators"]["builtin"] is True)
        pathologists, residents = by_name["Патологи"], by_name["Резиденты"]

        admin.post("/api/access", json={"folder_id": inner, "mode": "selected",
                                        "user_ids": [user_a_id], "group_ids": [pathologists]})
        check("пользователь вне группы доступа не получает", inner not in catalog_ids(bob)[0])
        admin.put(f"/api/groups/{pathologists}/members", json={"user_ids": [user_b_id]})
        check("после добавления в группу папка видна сразу", inner in catalog_ids(bob)[0])
        check("и её сканы тоже", "cccccccccccc" in catalog_ids(bob)[1])
        check("группа не открывает закрытый отдельно скан", "bbbbbbbbbbbb" not in catalog_ids(bob)[1])
        check("тайл через группу доступен", bob.get("/api/slides/cccccccccccc/tiles/8/0_0.jpg").status_code != 404)
        admin.put(f"/api/groups/{pathologists}/members", json={"user_ids": []})
        check("после исключения из группы доступ пропадает сразу", inner not in catalog_ids(bob)[0])
        check("тайл после исключения: 404", bob.get("/api/slides/cccccccccccc/tiles/8/0_0.jpg").status_code == 404)

        # Пользователь в двух группах: действует объединение разрешений
        admin.put(f"/api/groups/{pathologists}/members", json={"user_ids": [user_b_id]})
        admin.put(f"/api/groups/{residents}/members", json={"user_ids": [user_b_id]})
        check("в двух группах доступ по одной из них", inner in catalog_ids(bob)[0])
        admin.delete(f"/api/groups/{pathologists}")
        check("после удаления группы доступ через неё пропадает", inner not in catalog_ids(bob)[0])
        check("прямое разрешение другого пользователя не задето", inner in catalog_ids(alice)[0])
        check("разрешения удалённой группы стёрты",
              db.query_one("SELECT count(*) AS n FROM group_grants")["n"] == 0)

        check("название «Администраторы» зарезервировано",
              admin.post("/api/groups", json={"name": "администраторы"}).status_code == 400)
        check("повтор названия группы без учёта регистра отклоняется",
              admin.post("/api/groups", json={"name": "РЕЗИДЕНТЫ"}).status_code == 400)
        check("пользователь не управляет группами", alice.post("/api/groups", json={"name": "Х"}).status_code == 403)
        created_group = admin.post("/api/groups", json={"name": "Резиденты 2026"})
        check("администратор создаёт свою группу", created_group.status_code == 200)
        admin.post("/api/access", json={"folder_id": inner, "mode": "selected", "user_ids": [user_a_id]})

        # ---------- прямые запросы в обход каталога ----------
        for name, path in (
            ("сведения", "/api/slides/bbbbbbbbbbbb"),
            ("тайл", "/api/slides/bbbbbbbbbbbb/tiles/8/0_0.jpg"),
            ("миниатюра", "/api/slides/bbbbbbbbbbbb/thumbnail.jpg"),
            ("этикетка", "/api/slides/bbbbbbbbbbbb/label.jpg"),
        ):
            code = bob.get(path).status_code
            check(f"чужой скан, {name}: 404", code == 404, f"(получено {code})")

        check("несуществующий скан отвечает так же", bob.get("/api/slides/zzzzzzzzzzzz").status_code == 404)
        code = alice.get("/api/slides/aaaaaaaaaaaa").status_code
        check("свой скан доступен (файла нет, но не 404)", code != 404, f"(получено {code})")
        # Право на этикетку проверяется до чтения файла, поэтому 404 здесь означал бы отказ
        code = alice.get("/api/slides/aaaaaaaaaaaa/label.jpg").status_code
        check("этикетка своего скана не отклоняется по правам", code != 404, f"(получено {code})")

        # ---------- исходное имя файла ----------
        db.execute("UPDATE slides SET original_name = ? WHERE id = ?", ("Иванов_И_И.svs", "aaaaaaaaaaaa"))
        body = alice.get("/api/slides/aaaaaaaaaaaa").text
        check("пользователю не отдаётся имя файла", "Иванов" not in body and ".svs" not in body)
        check("администратору имя файла видно", "Иванов" in admin.get("/api/slides/aaaaaaaaaaaa").text)

        # ---------- память прав (СК-3): изменение действует со следующего запроса ----------
        tile_c = "/api/slides/cccccccccccc/tiles/8/0_0.jpg"
        check("тайл доступного скана отдаётся", alice.get(tile_c).status_code != 404)
        admin.post("/api/access", json={"folder_id": inner, "mode": "selected", "user_ids": []})
        code = alice.get(tile_c).status_code
        check("следующий же запрос тайла после отзыва доступа: 404", code == 404, f"(получено {code})")
        admin.post("/api/access", json={"folder_id": inner, "mode": "selected", "user_ids": [user_a_id]})
        check("после возврата доступа тайл снова отдаётся", alice.get(tile_c).status_code != 404)

        admin.patch(f"/api/users/{user_a_id}", json={"status": "blocked"})
        code = alice.get(tile_c).status_code
        check("следующий же запрос тайла после блокировки: 401", code == 401, f"(получено {code})")
        admin.patch(f"/api/users/{user_a_id}", json={"status": "active"})
        login(alice, "user-a")  # блокировка обрывает сессию, входим заново
        check("после разблокировки тайл снова отдаётся", alice.get(tile_c).status_code != 404)

        # Замер тем же способом, что в аудите: учётная запись, права, строка скана
        perms = app.state.perms
        durations = []
        for _ in range(200):
            started = time.perf_counter()
            row = perms.user(user_a_id)
            slide_row = db.query_one("SELECT * FROM slides WHERE id = ? AND missing = 0", ("cccccccccccc",))
            perms.index(row).can_view_slide(slide_row)
            durations.append((time.perf_counter() - started) * 1000)
        median = statistics.median(durations)
        check("проверка прав не дольше 1 мс по медиане", median <= 1.0, f"({median:.3f} мс)")

        # ---------- права администратора ----------
        check("пользователь не создаёт папки", alice.post("/api/folders", json={"name": "Чужая"}).status_code == 403)
        check("пользователь не видит журнал", alice.get("/api/journal").status_code == 403)
        check("пользователь не видит список учётных записей", alice.get("/api/users").status_code == 403)
        check("пользователь не удаляет сканы", alice.delete("/api/slides/aaaaaaaaaaaa").status_code == 403)

        # ---------- учётные записи ----------
        created = admin.post("/api/users", json={"login": "новый", "role": "user"})
        check("создание пользователя выдаёт пароль", created.status_code == 200 and len(created.json()["password"]) > 10)

        # Пароль можно задать вручную (решение заказчика 2026-09-19)
        manual = admin.post("/api/users", json={"login": "ручной", "role": "user", "password": "мой-пароль-123"})
        check("пароль можно задать вручную", manual.status_code == 200 and manual.json()["password"] == "мой-пароль-123")
        with_manual = TestClient(app)
        entered = with_manual.post("/api/login", json={"login": "ручной", "password": "мой-пароль-123"})
        check("вход с заданным вручную паролем", entered.status_code == 200)
        short = admin.post("/api/users", json={"login": "короткий", "role": "user", "password": "abc"})
        check("слишком короткий пароль отклоняется", short.status_code == 400, f"({short.json().get('detail', '')[:40]})")
        manual_id = manual.json()["id"]
        reset = admin.post(f"/api/users/{manual_id}/password", json={"password": "другой-пароль-1"})
        check("сброс пароля вручную", reset.status_code == 200 and reset.json()["password"] == "другой-пароль-1")
        check("прежняя сессия после сброса прекращается", with_manual.get("/api/catalog").status_code == 401)
        generated = admin.post(f"/api/users/{manual_id}/password", json={})
        check("сброс без пароля генерирует новый", generated.status_code == 200 and len(generated.json()["password"]) > 10)
        admin.delete(f"/api/users/{manual_id}")
        new_id = created.json()["id"]
        fresh = TestClient(app)
        first = fresh.post("/api/login", json={"login": "новый", "password": created.json()["password"]})
        check("вход с выданным паролем", first.status_code == 200)
        check("каталог открыт сразу, принудительной смены пароля нет", fresh.get("/api/catalog").status_code == 200)
        second_device = TestClient(app)
        second_device.post("/api/login", json={"login": "новый", "password": created.json()["password"]})
        check("та же запись на втором устройстве", second_device.get("/api/catalog").status_code == 200)
        changed = fresh.post(
            "/api/password",
            json={"current_password": created.json()["password"], "new_password": "мой-новый-пароль"},
        )
        check("пользователь меняет пароль сам", changed.status_code == 200)
        check("после смены каталог открыт", fresh.get("/api/catalog").status_code == 200)
        check("после смены пароля прежние сессии прекращаются", second_device.get("/api/catalog").status_code == 401)
        short = fresh.post(
            "/api/password", json={"current_password": "мой-новый-пароль", "new_password": "короткий"}
        )
        check("слишком короткий новый пароль отклоняется", short.status_code == 400)

        admin.patch(f"/api/users/{new_id}", json={"status": "blocked"})
        blocked = TestClient(app)
        code = blocked.post("/api/login", json={"login": "новый", "password": "мой-новый-пароль"}).status_code
        check("заблокированный не входит", code == 403, f"(получено {code})")

        # Блокировка действует немедленно, без ожидания конца сессии
        admin.patch(f"/api/users/{user_a_id}", json={"status": "blocked"})
        check("действующая сессия заблокированного прерывается", alice.get("/api/catalog").status_code == 401)
        admin.patch(f"/api/users/{user_a_id}", json={"status": "active"})

        admin_id = db.query_one("SELECT id FROM users WHERE login = 'admin-test'")["id"]
        check("последнего администратора нельзя удалить", admin.delete(f"/api/users/{admin_id}").status_code == 400)
        check("себя нельзя заблокировать", admin.patch(f"/api/users/{admin_id}", json={"status": "blocked"}).status_code == 400)

        # ---------- «что видит пользователь» ----------
        preview = {s["id"] for s in admin.get(f"/api/access/preview/{user_a_id}").json()["slides"]}
        check("предпросмотр доступа совпадает с каталогом", preview == {"aaaaaaaaaaaa", "cccccccccccc"}, f"({sorted(preview)})")

        # ---------- удаление папки ----------
        counts = admin.get(f"/api/folders/{inner}/contents").json()
        check("подсчёт содержимого папки", counts == {"folders": 0, "slides": 2}, f"({counts})")
        admin.delete(f"/api/folders/{inner}")
        check("папка удалена вместе со сканами", db.query_one("SELECT count(*) AS n FROM slides")["n"] == 1)

        # ---------- журнал ----------
        entries = admin.get("/api/journal").json()
        actions = {row["action"] for row in entries}
        check("в журнале есть вход", "login.ok" in actions)
        check("в журнале есть открытие скана", "slide.open" in actions)
        check("в журнале есть изменение доступа", "access.change" in actions)
        check("в журнале нет имени файла", all("Иванов" not in str(row) for row in entries))

        # ---------- аннотации (этап 9) ----------
        # user-a видит скан aaaaaaaaaaaa (папка top открыта всем), user-b — нет
        # Группу «Патологи» выше по тесту удаляли: заводим заново, если её нет
        known = admin.get("/api/groups").json()["groups"]
        pathologists_id = next((g["id"] for g in known if g["name"] == "Патологи"), None)
        if pathologists_id is None:
            pathologists_id = admin.post("/api/groups", json={"name": "Патологи"}).json()["id"]
        admin.put(f"/api/groups/{pathologists_id}/members", json={"user_ids": [user_a_id]})
        login(alice, "user-a")  # смена состава групп сбрасывает память прав

        info = alice.get("/api/slides/aaaaaaaaaaaa").json()
        check("патолог видит, что может размечать", info.get("can_annotate") is True)
        check("не патолог размечать не может",
              bob.get("/api/slides/aaaaaaaaaaaa").json().get("can_annotate") is False)

        point = alice.post("/api/slides/aaaaaaaaaaaa/annotations",
                           json={"kind": "point", "points": [[120.5, 240.25]], "comment": "митоз"})
        check("патолог ставит указатель", point.status_code == 200, f"({point.text[:60]})")
        point_id = point.json()["id"]
        check("координаты сохранены в пикселях скана", point.json()["points"] == [[120.5, 240.2]],
              f"({point.json()['points']})")
        check("автор записан", point.json()["author"] == "user-a")
        check("свою аннотацию автор может править", point.json()["can_edit"] is True)

        polygon = alice.post("/api/slides/aaaaaaaaaaaa/annotations",
                             json={"kind": "polygon", "points": [[10, 10], [80, 10], [80, 90], [10, 90]],
                                   "comment": "участок"})
        check("патолог обводит полигон", polygon.status_code == 200, f"({polygon.text[:60]})")
        polygon_id = polygon.json()["id"]

        short = alice.post("/api/slides/aaaaaaaaaaaa/annotations",
                           json={"kind": "polygon", "points": [[1, 1], [2, 2]], "comment": ""})
        check("полигон из двух точек отклоняется", short.status_code == 400, f"({short.text[:60]})")
        odd = alice.post("/api/slides/aaaaaaaaaaaa/annotations",
                         json={"kind": "circle", "points": [[1, 1]], "comment": ""})
        check("неизвестный вид аннотации отклоняется", odd.status_code == 400)
        long_comment = alice.post("/api/slides/aaaaaaaaaaaa/annotations",
                                  json={"kind": "point", "points": [[1, 1]], "comment": "я" * 1001})
        check("слишком длинный комментарий отклоняется", long_comment.status_code == 400)

        listed = alice.get("/api/slides/aaaaaaaaaaaa/annotations").json()
        check("обе аннотации в списке", {a["id"] for a in listed} == {point_id, polygon_id})

        # А-10: скан, к которому нет доступа (cccccccccccc виден только user-a),
        # не отдаёт ни аннотации, ни возможность их поставить
        check("аннотации чужого скана: 404",
              bob.get("/api/slides/cccccccccccc/annotations").status_code == 404)
        check("поставить аннотацию на чужой скан: 404",
              bob.post("/api/slides/cccccccccccc/annotations",
                       json={"kind": "point", "points": [[1, 1]], "comment": ""}).status_code == 404)

        # А-3: не патолог видит аннотации доступного скана, но поставить не может
        seen = bob.get("/api/slides/aaaaaaaaaaaa/annotations")
        check("не патолог видит список аннотаций доступного скана",
              seen.status_code == 200 and len(seen.json()) == 2, f"({seen.status_code})")
        check("чужие аннотации не правятся", all(a["can_edit"] is False for a in seen.json()))
        denied = bob.post("/api/slides/aaaaaaaaaaaa/annotations",
                          json={"kind": "point", "points": [[5, 5]], "comment": ""})
        check("не патолог получает отказ на создание (403)", denied.status_code == 403, f"({denied.text[:60]})")

        # А-14: правка своей аннотации
        moved = alice.patch(f"/api/annotations/{point_id}",
                            json={"points": [[300, 400]], "comment": "митоз, повтор"})
        check("автор двигает свою аннотацию и меняет комментарий",
              moved.status_code == 200 and moved.json()["points"] == [[300.0, 400.0]]
              and moved.json()["comment"] == "митоз, повтор", f"({moved.text[:70]})")

        # Цвет: зелёный по умолчанию, красный на выбор; правка цвета не трогает остальное
        check("новая аннотация неоново-зелёная", point.json()["color"] == "green", f"({point.json().get('color')})")
        recolored = alice.patch(f"/api/annotations/{point_id}", json={"color": "red"})
        check("автор перекрашивает аннотацию в красный",
              recolored.status_code == 200 and recolored.json()["color"] == "red"
              and recolored.json()["comment"] == "митоз, повтор" and recolored.json()["points"] == [[300.0, 400.0]],
              f"({recolored.text[:70]})")
        check("правка комментария цвет не сбрасывает",
              alice.patch(f"/api/annotations/{point_id}", json={"comment": "митоз"}).json()["color"] == "red")
        check("неизвестный цвет отклоняется",
              alice.patch(f"/api/annotations/{point_id}", json={"color": "blue"}).status_code == 400)
        check("порядок списка постоянный: по нему считаются номера",
              [a["id"] for a in alice.get("/api/slides/aaaaaaaaaaaa/annotations").json()] == [point_id, polygon_id])

        # Чужую не тронуть: делаем второго патолога
        make_user("user-c")
        user_c_id = db.query_one("SELECT id FROM users WHERE login = 'user-c'")["id"]
        admin.put(f"/api/groups/{pathologists_id}/members", json={"user_ids": [user_a_id, user_c_id]})
        admin.post("/api/access", json={"folder_id": top, "mode": "all"})
        carol = TestClient(app)
        login(carol, "user-c")
        foreign = carol.get(f"/api/slides/aaaaaaaaaaaa/annotations").json()
        check("чужая аннотация помечена как недоступная для правки",
              all(a["can_edit"] is False for a in foreign), f"({foreign[:1]})")
        check("чужую аннотацию патолог не правит",
              carol.patch(f"/api/annotations/{point_id}", json={"comment": "нет"}).status_code == 403)
        check("чужую аннотацию патолог не удаляет",
              carol.delete(f"/api/annotations/{point_id}").status_code == 403)
        check("администратор удаляет любую",
              admin.delete(f"/api/annotations/{polygon_id}").status_code == 200)

        # Журнал: вид и скан есть, текста комментария нет (А-8)
        journal = admin.get("/api/journal").json()
        marks = [row for row in journal if row["action"].startswith("annotation.")]
        check("создание, правка и удаление аннотаций в журнале",
              {row["action"] for row in marks} == {"annotation.create", "annotation.update", "annotation.delete"},
              f"({sorted({r['action'] for r in marks})})")
        check("текста комментария в журнале нет", all("митоз" not in str(row) for row in journal))

        # А-8: аннотации уходят вместе со сканом
        before = db.query_one("SELECT count(*) AS n FROM annotations")["n"]
        admin.delete("/api/slides/aaaaaaaaaaaa")
        after = db.query_one("SELECT count(*) AS n FROM annotations")["n"]
        check("удаление скана удаляет его аннотации", before > 0 and after == 0, f"({before} → {after})")
        make_slide("aaaaaaaaaaaa", top)  # возвращаем скан для остальных проверок

        # Удаление папки уносит и сканы, и их аннотации: папка удаляет сканы по
        # одному, а дальше срабатывает связь в базе
        doomed = admin.post("/api/folders", json={"name": "На удаление"}).json()["id"]
        admin.post("/api/access", json={"folder_id": doomed, "mode": "all"})
        make_slide("eeeeeeeeeeee", doomed)
        alice.post("/api/slides/eeeeeeeeeeee/annotations",
                   json={"kind": "point", "points": [[7, 7]], "comment": "в папке"})
        check("аннотация в папке создана",
              db.query_one("SELECT count(*) AS n FROM annotations WHERE slide_id = 'eeeeeeeeeeee'")["n"] == 1)
        admin.delete(f"/api/folders/{doomed}")
        check("удаление папки уносит аннотации её сканов",
              db.query_one("SELECT count(*) AS n FROM annotations WHERE slide_id = 'eeeeeeeeeeee'")["n"] == 0)

        # ---------- клеточность (этап 11, шаг 3): права, запуск, маски, выгрузка ----------
        square = [[100, 100], [900, 100], [900, 900], [100, 900]]
        check("состояние клеточности доступного скана: инструмента нет, окраска не H&E",
              alice.get("/api/slides/aaaaaaaaaaaa/cellularity").json()["available"] is False)
        check("клеточность чужого скана: 404 на состояние, запуск и маски",
              bob.get("/api/slides/cccccccccccc/cellularity").status_code == 404
              and bob.post("/api/slides/cccccccccccc/cellularity/runs", json={}).status_code == 404
              and bob.get("/api/slides/cccccccccccc/cellularity/runs/xxxx/masks/0/0_0.png").status_code == 404)
        check("запуск без группы «Патологи»: 403",
              bob.post("/api/slides/aaaaaaaaaaaa/cellularity/runs", json={}).status_code == 403)
        check("предложить контуры без группы: 403",
              bob.post("/api/slides/aaaaaaaaaaaa/cellularity/propose").status_code == 403)
        not_he = alice.post("/api/slides/aaaaaaaaaaaa/cellularity/runs", json={})
        check("запуск на скане без окраски H&E: 400", not_he.status_code == 400, f"({not_he.json().get('detail')})")

        # настоящий синтетический SVS с окраской H&E
        from synthetic_svs import build_svs
        (WORK_DIR / "slides" / "dd").mkdir(parents=True, exist_ok=True)
        build_svs(WORK_DIR / "slides" / "dd" / "dddddddddddd.svs", size=2048, objective=20, mpp=0.5)
        make_slide("dddddddddddd", top)
        db.execute("UPDATE slides SET stain = 'HE', mpp = 0.5, objective = 20, width = 2048, height = 2048 "
                   "WHERE id = 'dddddddddddd'")
        status = alice.get("/api/slides/dddddddddddd/cellularity").json()
        check("скан H&E с размером пикселя: инструмент есть, патолог может запускать",
              status["available"] is True and status["can_run"] is True and status["run"] is None)
        check("у пользователя без группы инструмент виден, запуск нет",
              bob.get("/api/slides/dddddddddddd/cellularity").json()["can_run"] is False)
        no_contours = alice.post("/api/slides/dddddddddddd/cellularity/runs", json={})
        check("запуск без контуров ткани: 400", no_contours.status_code == 400, f"({no_contours.json().get('detail')})")
        too_long = alice.post("/api/slides/dddddddddddd/annotations",
                              json={"kind": "tissue", "points": [[i, i] for i in range(4001)]})
        check("контур ткани не длиннее 4000 вершин", too_long.status_code == 400)
        check("контур ткани у пользователя без группы: 403",
              bob.post("/api/slides/dddddddddddd/annotations", json={"kind": "tissue", "points": square}).status_code == 403)
        tissue = alice.post("/api/slides/dddddddddddd/annotations", json={"kind": "tissue", "points": square}).json()
        artifact = alice.post("/api/slides/dddddddddddd/annotations",
                              json={"kind": "artifact", "points": [[300, 300], [400, 300], [400, 400], [300, 400]]}).json()
        check("контуры ткани и артефакта созданы как аннотации своих видов",
              tissue.get("kind") == "tissue" and artifact.get("kind") == "artifact")
        counts = alice.get("/api/slides/dddddddddddd/cellularity").json()["contours"]
        check("состояние считает контуры по видам", counts == {"tissue": 1, "artifact": 1}, f"({counts})")
        bad_res = alice.post("/api/slides/dddddddddddd/cellularity/runs", json={"resolution": "5"})
        check("неизвестное разрешение: 400", bad_res.status_code == 400)
        started = alice.post("/api/slides/dddddddddddd/cellularity/runs", json={"resolution": "original"}).json()
        check("расчёт поставлен в очередь", started.get("status") in ("queued", "running"), f"({started})")
        check("второй запуск, пока идёт первый: 409",
              carol.post("/api/slides/dddddddddddd/cellularity/runs", json={}).status_code == 409)
        check("маски до окончания расчёта: 404",
              alice.get(f"/api/slides/dddddddddddd/cellularity/runs/{started['id']}/masks/0/0_0.png").status_code == 404)
        deadline = time.time() + 180
        run = started
        while run["status"] in ("queued", "running") and time.time() < deadline:
            time.sleep(0.5)
            run = alice.get(f"/api/slides/dddddddddddd/cellularity/runs/{started['id']}").json()
        check("расчёт завершился", run["status"] == "done", f"({run['status']}: {run.get('error')})")
        result = run.get("result") or {}
        fragment = (result.get("fragments") or [{}])[0]
        check("результат: площадь ткани по контуру 0,16 мм², артефакт 0,0025 мм², итог есть",
              abs(fragment.get("tissue_mm2", 0) - 0.16) < 1e-6 and abs(fragment.get("artifacts_mm2", 0) - 0.0025) < 1e-6
              and result.get("total") is not None and run["pixel_um"] == 2.0 and run["stale"] is False,
              f"({fragment.get('tissue_mm2')}, {fragment.get('artifacts_mm2')})")
        check("пик памяти и время процесса записаны", (run.get("peak_rss_mb") or 0) > 20 and (run.get("elapsed_s") or 0) > 0)
        seen_by_bob = bob.get(f"/api/slides/dddddddddddd/cellularity/runs/{started['id']}").json()
        check("результат видит пользователь без группы (доступ к скану есть)", seen_by_bob.get("status") == "done")
        tile = bob.get(f"/api/slides/dddddddddddd/cellularity/runs/{started['id']}/masks/10/0_0.png")
        check("тайл масок отдаётся как PNG с кэшем", tile.status_code == 200 and tile.headers["content-type"] == "image/png"
              and "immutable" in tile.headers.get("cache-control", ""))
        import io as _io
        from PIL import Image as _Image
        image = _Image.open(_io.BytesIO(tile.content))
        pixels = image.convert("RGBA")
        # уровень 10 — 1024 точки на 2048: контур 100…900 уровня 0 = 50…450 тайла
        inside = pixels.getpixel((200, 200))
        outside = pixels.getpixel((20, 20))
        check("внутри контура маска непрозрачна, вне контура прозрачна",
              image.mode == "RGBA" and inside[3] == 255 and outside[3] == 0, f"({inside}, {outside}, {image.size})")
        check("тайл масок за краем: 404",
              alice.get(f"/api/slides/dddddddddddd/cellularity/runs/{started['id']}/masks/10/9_9.png").status_code == 404)
        alice.patch(f"/api/annotations/{tissue['id']}", json={"points": [[100, 100], [950, 100], [950, 950], [100, 950]]})
        check("после правки контура результат помечен как устаревший",
              alice.get("/api/slides/dddddddddddd/cellularity").json()["run"]["stale"] is True)
        check("отмена завершённого расчёта: 400",
              alice.post(f"/api/slides/dddddddddddd/cellularity/runs/{started['id']}/cancel").status_code == 400)
        csv_text = admin.get("/api/cellularity/export.csv")
        check("выгрузка CSV администратору: расчёт есть, имени файла нет",
              csv_text.status_code == 200 and started["id"] in csv_text.text and "dddddddddddd" in csv_text.text
              and ".svs" not in csv_text.text and "итог" in csv_text.text)
        check("выгрузка CSV пользователю: 403", alice.get("/api/cellularity/export.csv").status_code == 403)
        journal = admin.get("/api/journal").json()
        cell_actions = {row["action"] for row in journal if row["action"].startswith("cellularity.")}
        check("журнал: запуск и итог расчёта", cell_actions == {"cellularity.start", "cellularity.done"}, f"({cell_actions})")
        listed = alice.get("/api/slides/dddddddddddd/annotations").json()
        check("список аннотаций отдаёт контуры с их видами",
              {a["kind"] for a in listed} == {"tissue", "artifact"})
        # все контуры разом: патолог убирает свои, чужие остаются; администратор — любые
        carol.post("/api/slides/dddddddddddd/annotations", json={"kind": "tissue", "points": square})
        check("удаление всех контуров без группы: 403",
              bob.delete("/api/slides/dddddddddddd/cellularity/contours").status_code == 403)
        cleared = alice.delete("/api/slides/dddddddddddd/cellularity/contours").json()
        check("патолог удалил свои контуры (ткань и артефакт), чужой остался",
              cleared == {"deleted": 2, "kept": 1}
              and alice.get("/api/slides/dddddddddddd/cellularity").json()["contours"] == {"tissue": 1, "artifact": 0},
              f"({cleared})")
        check("администратор удаляет и чужие контуры",
              admin.delete("/api/slides/dddddddddddd/cellularity/contours").json() == {"deleted": 1, "kept": 0}
              and alice.get("/api/slides/dddddddddddd/cellularity").json()["contours"] == {"tissue": 0, "artifact": 0})
        check("удаление расчёта без группы: 403",
              bob.delete(f"/api/slides/dddddddddddd/cellularity/runs/{started['id']}").status_code == 403)
        check("удаление расчёта патологом: 200, запись и карты классов исчезли, состояние без расчёта",
              alice.delete(f"/api/slides/dddddddddddd/cellularity/runs/{started['id']}").status_code == 200
              and db.query_one("SELECT count(*) AS n FROM cellularity_runs WHERE id = ?", (started["id"],))["n"] == 0
              and not (WORK_DIR / "data" / "cellularity" / "dddddddddddd" / started["id"]).exists()
              and alice.get("/api/slides/dddddddddddd/cellularity").json()["run"] is None)
        check("журнал: удаление расчёта",
              any(row["action"] == "cellularity.delete" for row in admin.get("/api/journal").json()))
        # фоновый расчёт после загрузки (решение заказчика 2026-09-24): контуры есть — расчёт
        # стартует сам с разрешением из настроек; при свежем результате повторно не стартует
        from dataclasses import replace
        service = app.state.cellularity
        service.config = replace(service.config, auto=True)
        alice.post("/api/slides/dddddddddddd/annotations", json={"kind": "tissue", "points": square})
        slide_row = db.query_one("SELECT * FROM slides WHERE id = 'dddddddddddd'")
        actor = db.query_one("SELECT * FROM users WHERE login = 'user-a'")
        service.auto_run(slide_row, actor)
        deadline = time.time() + 180
        auto = None
        while time.time() < deadline:
            time.sleep(0.5)
            auto = alice.get("/api/slides/dddddddddddd/cellularity").json()["run"]
            if auto and auto["status"] not in ("queued", "running"):
                break
        check("фоновый расчёт запустился сам и завершился при разрешении из настроек",
              auto is not None and auto["status"] == "done" and auto["resolution"] == service.default_resolution,
              f"({auto and auto['status']}, {auto and auto['resolution']})")
        before = db.query_one("SELECT count(*) AS n FROM cellularity_runs WHERE slide_id = 'dddddddddddd'")["n"]
        service.auto_run(slide_row, actor)
        time.sleep(1)
        check("при свежем результате фоновый расчёт повторно не запускается",
              db.query_one("SELECT count(*) AS n FROM cellularity_runs WHERE slide_id = 'dddddddddddd'")["n"] == before)
        check("журнал помечает автоматический запуск",
              any(row["action"] == "cellularity.start" and "автоматически" in (row["detail"] or "")
                  for row in admin.get("/api/journal").json()))
        one_click = alice.post("/api/slides/dddddddddddd/cellularity/runs", json={"propose": True}).json()
        check("запуск одной кнопкой: разрешение из настроек, контуры уже есть",
              one_click.get("status") in ("queued", "running") and one_click.get("resolution") == service.default_resolution)
        while alice.get(f"/api/slides/dddddddddddd/cellularity/runs/{one_click['id']}").json()["status"] in ("queued", "running"):
            time.sleep(0.5)
        service.config = replace(service.config, auto=False)
        admin.delete("/api/slides/dddddddddddd")
        check("удаление скана уносит расчёты и карты классов",
              db.query_one("SELECT count(*) AS n FROM cellularity_runs WHERE slide_id = 'dddddddddddd'")["n"] == 0
              and not (WORK_DIR / "data" / "cellularity" / "dddddddddddd").exists())

        # ---------- перемещение папки (КД-4) ----------
        moved = admin.post("/api/folders", json={"name": "Перенос", "parent_id": top}).json()["id"]
        other = admin.post("/api/folders", json={"name": "2026-09-20"}).json()["id"]
        make_slide("dddddddddddd", moved)

        response = admin.patch(f"/api/folders/{moved}", json={"parent_id": other, "move": True})
        check("папка переносится в другую папку", response.status_code == 200, f"({response.text[:60]})")
        check("после переноса родитель сменился",
              db.query_one("SELECT parent_id FROM folders WHERE id = ?", (moved,))["parent_id"] == other)
        check("скан остался в перенесённой папке",
              db.query_one("SELECT folder_id FROM slides WHERE id = 'dddddddddddd'")["folder_id"] == moved)
        check("ссылка на скан после переноса работает", admin.get("/api/slides/dddddddddddd").status_code == 200)

        into_self = admin.patch(f"/api/folders/{moved}", json={"parent_id": moved, "move": True})
        check("папку нельзя перенести внутрь себя", into_self.status_code == 400, f"({into_self.text[:60]})")

        inner_of_moved = admin.post("/api/folders", json={"name": "Внутренняя", "parent_id": moved}).json()["id"]
        into_child = admin.patch(f"/api/folders/{moved}", json={"parent_id": inner_of_moved, "move": True})
        check("папку нельзя перенести в свою же подпапку", into_child.status_code == 400)

        to_root = admin.patch(f"/api/folders/{moved}", json={"parent_id": None, "move": True})
        check("без своего режима доступа папка не выносится в корень",
              to_root.status_code == 400 and "режим" in to_root.text, f"({to_root.text[:70]})")
        admin.post("/api/access", json={"folder_id": moved, "mode": "admins"})
        to_root = admin.patch(f"/api/folders/{moved}", json={"parent_id": None, "move": True})
        check("со своим режимом доступа папка выносится в корень", to_root.status_code == 200)

        # Глубина: в цепочку из пяти уровней двухуровневую ветку не вставить
        deep_chain = admin.post("/api/folders", json={"name": "Глубина"}).json()["id"]
        for level in range(2, 5):
            deep_chain = admin.post("/api/folders", json={"name": f"Г{level}", "parent_id": deep_chain}).json()["id"]
        too_deep = admin.patch(f"/api/folders/{moved}", json={"parent_id": deep_chain, "move": True})
        check("перенос, который превысит пять уровней, отклоняется",
              too_deep.status_code == 400, f"({too_deep.text[:70]})")

        actions = [row["action"] for row in admin.get("/api/journal").json()]
        check("перемещение папки записано в журнал", "folder.move" in actions)

        # ---------- вложенность и названия ----------
        deep = top
        for level in range(2, 7):
            response = admin.post("/api/folders", json={"name": f"Уровень {level}", "parent_id": deep})
            if response.status_code != 200:
                break
            deep = response.json()["id"]
        check("глубже пяти уровней папки не создаются", level == 6 and response.status_code == 400, f"({response.text[:60]})")
        same = admin.post("/api/folders", json={"name": "2026-09-19"})
        check("повтор названия в одной папке отклоняется", same.status_code == 400)

        # ---------- окраска: код из списка, маркер у ИГХ (ОК-1…ОК-3) ----------
        make_slide("ssssssssssss", top)

        def set_stain(client, **fields):
            body = {"title": "Скан окраски", "note": "", **fields}
            return client.patch("/api/slides/ssssssssssss", json=body)

        def stain_of():
            info = admin.get("/api/slides/ssssssssssss").json()
            return info["stain"], info["ihc_marker"]

        check("H&E сохраняется кодом", set_stain(admin, stain="HE", ihc_marker="").status_code == 200
              and stain_of() == ("HE", None), f"({stain_of()})")
        set_stain(admin, stain="IHC", ihc_marker="  Ki-67 ")
        check("у ИГХ сохраняется маркер", stain_of() == ("IHC", "Ki-67"), f"({stain_of()})")
        set_stain(admin, stain="CONGO", ihc_marker="CD3")
        check("у окраски не ИГХ маркер не хранится", stain_of() == ("CONGO", None), f"({stain_of()})")
        check("неизвестная окраска отклоняется", set_stain(admin, stain="PAS").status_code == 400)
        check("слишком длинный маркер отклоняется", set_stain(admin, stain="IHC", ihc_marker="x" * 61).status_code == 400)
        set_stain(admin, stain="", ihc_marker="")
        check("окраску можно снять", stain_of() == (None, None), f"({stain_of()})")
        check("пользователь окраску не меняет", set_stain(alice, stain="HE").status_code == 403)
        db.execute("UPDATE slides SET case_code = 'P1', glass = 'S1', title = NULL, stain = 'IHC', "
                   "ihc_marker = 'CD3' WHERE id = 'ssssssssssss'")
        check("в названии по умолчанию у ИГХ только маркер",
              admin.get("/api/slides/ssssssssssss").json()["title"] == "P1 · S1 · CD3")


def check_stain_migration() -> None:
    """База версии 5 (окраска свободным текстом, аннотации только двух видов, без
    расчётов клеточности) переносится на текущую версию (ОК-3, КЛ-2, КЛ-8)."""
    import sqlite3
    from server.db import SCHEMA_VERSION, Database

    path = WORK_DIR / "v5.sqlite3"
    Database(path).close_all()
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("ALTER TABLE slides DROP COLUMN ihc_marker")
    # как было до версии 7: виды аннотаций только point и polygon, таблицы расчётов нет
    conn.executescript("""
        DROP TABLE cellularity_runs;
        DROP TABLE annotations;
        CREATE TABLE annotations (
            id TEXT PRIMARY KEY, slide_id TEXT NOT NULL REFERENCES slides(id) ON DELETE CASCADE,
            kind TEXT NOT NULL CHECK (kind IN ('point', 'polygon')), points TEXT NOT NULL,
            comment TEXT NOT NULL DEFAULT '', color TEXT NOT NULL DEFAULT 'green',
            author_id INTEGER REFERENCES users(id) ON DELETE SET NULL, author TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
        CREATE INDEX annotations_slide ON annotations(slide_id);
    """)
    conn.execute("INSERT INTO folders (id, name, access_mode) VALUES (1, 'Корень', 'admins')")
    for slide_id, stain in (("m1", "HE"), ("m2", "Ki-67"), ("m3", None), ("m4", "  ALK (D5F3) ")):
        conn.execute("INSERT INTO slides (id, key, folder_id, size, mtime, width, height, stain) "
                     "VALUES (?, ?, 1, 1, 0, 1, 1, ?)", (slide_id, f"{slide_id}.svs", stain))
    conn.execute("INSERT INTO annotations (id, slide_id, kind, points, comment, author) "
                 "VALUES ('a1', 'm1', 'polygon', '[[1,1],[2,1],[2,2]]', 'старая', 'кто-то')")
    conn.execute("PRAGMA user_version = 5")
    conn.commit()
    conn.close()

    migrated = Database(path)
    rows = {r["id"]: (r["stain"], r["ihc_marker"]) for r in migrated.query("SELECT id, stain, ihc_marker FROM slides")}
    version = migrated.query_one("PRAGMA user_version")[0]
    kept = migrated.query_one("SELECT kind, comment, author FROM annotations WHERE id = 'a1'")
    migrated.execute("INSERT INTO annotations (id, slide_id, kind, points, author) VALUES ('a2', 'm1', 'tissue', '[[0,0],[9,0],[9,9]]', 'x')")
    contour = migrated.query_one("SELECT kind FROM annotations WHERE id = 'a2'")["kind"]
    runs_table = migrated.query_one("SELECT count(*) AS n FROM cellularity_runs")["n"]
    migrated.close_all()
    check(f"перенос базы 5 → {SCHEMA_VERSION}: схема обновлена", version == SCHEMA_VERSION, f"({version})")
    check("перенос базы 5 → 6: окраски разложены",
          rows == {"m1": ("HE", None), "m2": ("IHC", "Ki-67"), "m3": (None, None), "m4": ("IHC", "ALK (D5F3)")},
          f"({rows})")
    check("перенос базы 6 → 7: аннотация сохранена, контур «ткань» принимается, таблица расчётов есть",
          kept is not None and tuple(kept) == ("polygon", "старая", "кто-то") and contour == "tissue" and runs_table == 0)


if __name__ == "__main__":
    try:
        main()
        check_stain_migration()
    finally:
        shutil.rmtree(WORK_DIR, ignore_errors=True)
    if failures:
        print(f"\nНе пройдено: {len(failures)}")
        for name in failures:
            print(f"  - {name}")
        raise SystemExit(1)
    print("\nВсе проверки доступа пройдены")
