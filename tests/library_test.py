"""Папки со сканами настольной программы (docs/TOR-desktop.md, этап 2) на временных папках.

Запуск:  .venv\\Scripts\\python.exe -X utf8 tests\\library_test.py

Сканы синтетические (tests/synthetic_svs.py) и лежат во временной папке с
кириллицей в пути — как у врача. Проверяется, что каталог повторяет диск,
скан узнаётся после переименования и переноса, пропавший файл помечается, а
файлы на диске не меняются ни одной операцией (НП-12…НП-17).
"""
from __future__ import annotations

import hashlib
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

WORK_DIR = Path(tempfile.mkdtemp(prefix="viewer-library-test-"))
(WORK_DIR / "config.yaml").write_text(
    f"data_dir: {(WORK_DIR / 'data').as_posix()!r}\ndesktop: true\ncellularity:\n  auto: false\n",
    encoding="utf-8",
)
os.environ["VIEWER_CONFIG"] = str(WORK_DIR / "config.yaml")
os.environ["VIEWER_SECRET_KEY"] = "test-secret-key"

from fastapi.testclient import TestClient  # noqa: E402

from server.main import app  # noqa: E402
from synthetic_svs import build_svs  # noqa: E402

db = app.state.db
library = app.state.library
failures: list[str] = []

SCANS = WORK_DIR / "Сканы пациентов"
OUTSIDE = WORK_DIR / "вне папки"


def check(name: str, condition: bool, detail: str = "") -> None:
    print(f"[{'OK' if condition else 'СБОЙ'}] {name} {detail}")
    if not condition:
        failures.append(name)


def state_of_files(root: Path) -> dict[str, tuple[int, float, str]]:
    """Размер, дата и сумма каждого файла: программа не должна менять ни одного."""
    result = {}
    for path in root.rglob("*"):
        if path.is_file():
            stat = path.stat()
            result[str(path.relative_to(root))] = (stat.st_size, stat.st_mtime,
                                                   hashlib.sha256(path.read_bytes()).hexdigest())
    return result


def catalog(client: TestClient) -> dict:
    return client.get("/api/catalog").json()


def by_title(data: dict) -> dict[str, dict]:
    return {s["title"]: s for s in data["slides"]}


def folder_names(data: dict) -> set[str]:
    return {f["name"] for f in data["folders"]}


def sync(client: TestClient) -> dict:
    return client.post("/api/library/sync", json={}).json()


def main() -> None:
    (SCANS / "Случай 1" / "Окраски").mkdir(parents=True)
    (SCANS / "Пустая папка").mkdir()
    OUTSIDE.mkdir()
    build_svs(SCANS / "Случай 1" / "Биопсия HE.svs", size=1024, objective=20)
    build_svs(SCANS / "Случай 1" / "Окраски" / "Ki-67.svs", size=1024, objective=40)
    build_svs(SCANS / "Отдельный.svs", size=768, objective=20)
    (SCANS / "Случай 1" / "Повреждённый.svs").write_bytes(os.urandom(4096))
    (SCANS / "Случай 1" / "заметки.txt").write_text("не скан", encoding="utf-8")
    disk_before = state_of_files(WORK_DIR / "Сканы пациентов")

    with TestClient(app, base_url="http://127.0.0.1") as client:
        client.get(f"/desktop/enter?key={app.state.launch_keys.issue()}")

        # ---------- подключение папки ----------
        response = client.post("/api/library/sources", json={"path": str(SCANS)})
        check("папка с кириллицей в пути подключается", response.status_code == 200, response.text[:120])
        root_id = response.json().get("id")
        data = catalog(client)
        titles = by_title(data)
        check("сканы в каталоге под именами файлов (НП-6)",
              set(titles) == {"Биопсия HE", "Ki-67", "Отдельный"}, str(sorted(titles)))
        check("дерево повторяет диск: подпапки на месте, пустой папки нет (НП-12)",
              folder_names(data) == {"Сканы пациентов", "Случай 1", "Окраски"}, str(folder_names(data)))
        root = next(f for f in data["folders"] if f["id"] == root_id)
        check("подключённая папка помечена и знает путь на диске",
              root["is_source"] is True and root["path"] == str(SCANS.resolve()))
        check("окраска распознана по имени файла", titles["Биопсия HE"]["stain"] == "HE", str(titles["Биопсия HE"]["stain"]))
        check("увеличение прочитано из файла", titles["Ki-67"]["objective"] == 40)
        problems = data["problems"]
        check("повреждённый файл виден с причиной, в сканы не попал (НП-17)",
              [p["name"] for p in problems] == ["Повреждённый.svs"] and "не распознан" in problems[0]["reason"],
              str(problems))
        check("файлы не-сканы не видны", all("заметки" not in s["title"] for s in data["slides"]))

        he = titles["Биопсия HE"]
        info = client.get(f"/api/slides/{he['id']}").json()
        check("в сведениях о скане нет пути к файлу",
              all(str(SCANS) not in str(v) for k, v in info.items() if k != "path"), str(list(info)))
        check("тайл скана из папки с кириллицей отдаётся",
              client.get(f"/api/slides/{he['id']}/tiles/0/0_0.jpg").status_code == 200)
        check("миниатюра отдаётся", client.get(f"/api/slides/{he['id']}/thumbnail.jpg").status_code == 200)

        again = client.post("/api/library/sources", json={"path": str(SCANS)})
        check("ту же папку второй раз не добавить", again.status_code == 400, again.text[:80])
        nested = client.post("/api/library/sources", json={"path": str(SCANS / "Случай 1")})
        check("вложенную в добавленную не добавить", nested.status_code == 400, nested.text[:80])
        check("несуществующую не добавить",
              client.post("/api/library/sources", json={"path": str(WORK_DIR / "нет такой")}).status_code == 400)

        # ---------- правки в программе ----------
        annotation = client.post(f"/api/slides/{he['id']}/annotations",
                                 json={"kind": "point", "points": [[10, 10]], "comment": "метка"})
        check("аннотация ставится", annotation.status_code == 200)
        check("название и окраска меняются", client.patch(
            f"/api/slides/{titles['Отдельный']['id']}", json={"title": "Мой скан", "stain": "CONGO"}).status_code == 200)
        check("новой папки в программе не создать",
              client.post("/api/folders", json={"name": "x", "parent_id": root_id}).status_code == 404)
        check("папку не переименовать", client.patch(f"/api/folders/{root_id}", json={"name": "y"}).status_code == 404)
        check("папку не удалить", client.delete(f"/api/folders/{root_id}").status_code == 404)
        check("скан не удалить", client.delete(f"/api/slides/{he['id']}").status_code == 404)
        check("скан не переместить",
              client.patch(f"/api/slides/{he['id']}", json={"folder_id": root_id}).status_code == 404)
        check("загрузки нет", client.post(
            "/api/uploads", json={"folder_id": root_id, "name": "a.svs", "size": 10}).status_code == 404)
        check("после всех действий в программе файлы на диске не изменились (НП-13)",
              state_of_files(SCANS) == disk_before)

        # ---------- переименование и перенос на диске ----------
        (SCANS / "Случай 1" / "Биопсия HE.svs").rename(SCANS / "Случай 1" / "Биопсия, стекло 2 HE.svs")
        sync(client)
        titles = by_title(catalog(client))
        renamed = titles.get("Биопсия, стекло 2 HE")
        check("переименованный файл — тот же скан (НП-15)", renamed is not None and renamed["id"] == he["id"],
              str(sorted(titles)))
        notes = client.get(f"/api/slides/{he['id']}/annotations").json()
        check("аннотация осталась при нём", len(notes) == 1 and notes[0]["comment"] == "метка")

        (SCANS / "Новый случай").mkdir()
        (SCANS / "Случай 1" / "Биопсия, стекло 2 HE.svs").rename(SCANS / "Новый случай" / "Перенесённый.svs")
        sync(client)
        data = catalog(client)
        moved = by_title(data).get("Перенесённый")
        new_folder = next((f for f in data["folders"] if f["name"] == "Новый случай"), None)
        check("перенесённый в другую подпапку — тот же скан, в новой папке",
              moved is not None and moved["id"] == he["id"] and new_folder and moved["folder_id"] == new_folder["id"])
        check("своё название переживает сверку", "Мой скан" in by_title(data))

        # ---------- пропажа и возврат ----------
        shutil.move(str(SCANS / "Случай 1" / "Окраски" / "Ki-67.svs"), str(OUTSIDE / "Ki-67.svs"))
        sync(client)
        data = catalog(client)
        gone = by_title(data).get("Ki-67")
        check("пропавший файл виден с пометкой (НП-14)", gone is not None and gone.get("missing") is True)
        check("папка пропавшего скана осталась", "Окраски" in folder_names(data))
        check("тайл пропавшего скана не отдаётся",
              client.get(f"/api/slides/{gone['id']}/tiles/0/0_0.jpg").status_code == 404)
        shutil.move(str(OUTSIDE / "Ki-67.svs"), str(SCANS / "Случай 1" / "Окраски" / "Ki-67.svs"))
        sync(client)
        back = by_title(catalog(client)).get("Ki-67")
        check("вернувшийся файл — тот же скан без пометки",
              back is not None and back["id"] == gone["id"] and not back.get("missing"))

        # ---------- копия файла ----------
        shutil.copy2(SCANS / "Отдельный.svs", SCANS / "Отдельный — копия.svs")
        sync(client)
        titles = by_title(catalog(client))
        check("копия файла — отдельный скан", "Отдельный — копия" in titles and "Мой скан" in titles
              and titles["Отдельный — копия"]["id"] != titles["Мой скан"]["id"])

        # ---------- отключение и повторное подключение ----------
        snapshot = state_of_files(SCANS)
        removed = client.delete(f"/api/library/sources/{root_id}")
        data = catalog(client)
        check("отключённая папка ушла из каталога", removed.status_code == 200 and not data["folders"]
              and not data["slides"], str(folder_names(data)))
        check("отключение не тронуло файлы", state_of_files(SCANS) == snapshot)
        root_id = client.post("/api/library/sources", json={"path": str(SCANS)}).json()["id"]
        titles = by_title(catalog(client))
        check("после повторного подключения сканы те же, аннотация на месте",
              titles.get("Перенесённый", {}).get("id") == he["id"]
              and len(client.get(f"/api/slides/{he['id']}/annotations").json()) == 1)
        check("и своё название тоже", "Мой скан" in titles)

        # ---------- две папки с одним именем ----------
        twin = WORK_DIR / "другой диск" / "Сканы пациентов"
        twin.mkdir(parents=True)
        build_svs(twin / "Второй.svs", size=512)
        response = client.post("/api/library/sources", json={"path": str(twin)})
        roots = [f["name"] for f in catalog(client)["folders"] if f["parent_id"] is None]
        check("одноимённые папки различаются в дереве", response.status_code == 200 and len(set(roots)) == 2,
              str(roots))

        # ---------- недоступная папка ----------
        shutil.move(str(twin), str(WORK_DIR / "другой диск" / "отключён"))
        stats = sync(client)
        second = by_title(catalog(client)).get("Второй")
        check("недоступная папка: сканы помечены, каталог не падает",
              stats.get("unavailable") == 1 and second is not None and second.get("missing") is True, str(stats))

        # ---------- длинный путь ----------
        deep = SCANS
        while len(str(deep)) < 250:
            deep = deep / "вложенная папка"
        try:
            deep.mkdir(parents=True)
            build_svs(deep / "Глубоко.svs", size=512)
            created = True
        except OSError as exc:
            created = False
            print(f"       (длинный путь не создан системой: {exc.__class__.__name__})")
        if created:
            sync(client)
            data = catalog(client)
            deep_slide = by_title(data).get("Глубоко")
            deep_problem = [p for p in data["problems"] if p["name"] == "Глубоко.svs"]
            opened = deep_slide is not None and client.get(
                f"/api/slides/{deep_slide['id']}/tiles/0/0_0.jpg").status_code == 200
            check(f"скан по пути длиннее 260 знаков ({len(str(deep / 'Глубоко.svs'))}) открывается или виден с причиной",
                  opened or bool(deep_problem), "открылся" if opened else f"причина: {deep_problem}")

        check("файлы на диске ни разу не изменены программой",
              all(state_of_files(SCANS).get(name) == value for name, value in snapshot.items()))


if __name__ == "__main__":
    try:
        main()
    finally:
        db.close_all()
        shutil.rmtree(WORK_DIR, ignore_errors=True)
    print(f"\nПроверок с ошибкой: {len(failures)}" if failures else "\nВсе проверки папок пройдены")
    sys.exit(1 if failures else 0)
