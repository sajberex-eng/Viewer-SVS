"""Проверка загрузки сканов на временной базе и временном хранилище.

Запуск:  .venv\\Scripts\\python.exe -X utf8 tests\\upload_test.py

Рабочая база и ваши сканы не затрагиваются: файл SVS собирается прямо в тесте
(tests\\synthetic_svs.py), в нём нет ни персональных данных, ни этикетки.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import sys
import tempfile
import time
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
warnings.filterwarnings("ignore")

WORK_DIR = Path(tempfile.mkdtemp(prefix="viewer-upload-test-"))
(WORK_DIR / "config.yaml").write_text(
    f"storage:\n  root: {(WORK_DIR / 'slides').as_posix()!r}\n"
    f"data_dir: {(WORK_DIR / 'data').as_posix()!r}\n"
    "auth:\n  https_only: false\n",
    encoding="utf-8",
)
os.environ["VIEWER_CONFIG"] = str(WORK_DIR / "config.yaml")
os.environ["VIEWER_SECRET_KEY"] = "test-secret-key"

from fastapi.testclient import TestClient  # noqa: E402

from server.auth import hash_password  # noqa: E402
from server.config import StorageConfig  # noqa: E402
from server import kfb  # noqa: E402
from server.main import app  # noqa: E402
from server.storage import DiskSpace, LocalFolderStorage, NotEnoughSpace  # noqa: E402
from server.uploads import STALE_DAYS  # noqa: E402
from synthetic_svs import build_svs  # noqa: E402

db = app.state.db
failures: list[str] = []
SECRET_NAME = "Иванов_Иван_Иванович.svs"  # имя с фамилией: наружу попадать не должно


def check(name: str, condition: bool, detail: str = "") -> None:
    print(f"[{'OK' if condition else 'СБОЙ'}] {name} {detail}")
    if not condition:
        failures.append(name)


def make_user(login: str, role: str) -> None:
    db.execute(
        "INSERT INTO users (login, password_hash, role, name) VALUES (?, ?, ?, ?)",
        (login, hash_password("password-1234"), role, login),
    )


def login(client: TestClient, name: str) -> None:
    assert client.post("/api/login", json={"login": name, "password": "password-1234"}).status_code == 200


def send(client: TestClient, upload_id: str, data: bytes, offset: int, checksum: bool = True):
    headers = {"x-part-sha256": hashlib.sha256(data).hexdigest()} if checksum else {}
    return client.put(f"/api/uploads/{upload_id}?offset={offset}", content=data, headers=headers)


def files_on_disk(root: Path) -> list[Path]:
    return [p for p in root.rglob("*") if p.is_file()]


def main() -> None:
    svs_path = build_svs(WORK_DIR / "source.svs")
    payload = svs_path.read_bytes()
    make_user("admin-test", "admin")
    make_user("user-a", "user")
    storage_root = WORK_DIR / "slides"
    uploads_dir = WORK_DIR / "data" / "uploads"

    with TestClient(app) as admin:
        alice = TestClient(app)
        login(admin, "admin-test")
        login(alice, "user-a")

        folder = admin.post("/api/folders", json={"name": "2026-09-19"}).json()["id"]

        # ---------- кто может и что можно ----------
        body = {"folder_id": folder, "name": SECRET_NAME, "size": len(payload)}
        check("пользователь не начинает загрузку", alice.post("/api/uploads", json=body).status_code == 403)
        check("не скан (.jpg) отклоняется",
              admin.post("/api/uploads", json={**body, "name": "photo.jpg"}).status_code == 400)
        check("многофайловый формат (.mrxs) отклоняется",
              admin.post("/api/uploads", json={**body, "name": "scan.mrxs"}).status_code == 400)
        kfb_start = admin.post("/api/uploads", json={**body, "name": "scan.kfb"})
        if kfb.AVAILABLE:  # образ Docker: KFB принимается
            check("KFB принимается там, где установлено чтение KFB", kfb_start.status_code == 200)
            admin.delete(f"/api/uploads/{kfb_start.json()['id']}")
        else:  # Windows: библиотек KFBio нет
            check("без чтения KFB загрузка .kfb отклоняется сразу, до передачи",
                  kfb_start.status_code == 400 and "KFB" in kfb_start.text)
        check("пустой файл отклоняется", admin.post("/api/uploads", json={**body, "size": 0}).status_code == 400)
        check("несуществующая папка отклоняется",
              admin.post("/api/uploads", json={**body, "folder_id": 9999}).status_code == 400)
        too_big = admin.post("/api/uploads", json={**body, "size": 5 * 10**9})
        check("файл больше допустимого отклоняется", too_big.status_code == 400 and "больше" in too_big.text)

        # ---------- загрузка частями и докачка ----------
        state = admin.post("/api/uploads", json=body).json()
        upload_id = state["id"]
        check("загрузка начата с нуля", state["received"] == 0, f"({state['size']} байт)")

        part = 2000
        first = send(admin, upload_id, payload[:part], 0)
        check("первая часть принята", first.status_code == 200 and first.json()["received"] == part)

        wrong = send(admin, upload_id, payload[part * 2 : part * 3], part * 2)
        # 409: повтором лечится — страница сверяется с сервером и продолжает (ЗГ-1)
        check("часть не на своём месте отклоняется (409) и называет нужный байт",
              wrong.status_code == 409 and str(part) in wrong.text, f"({wrong.json()['detail'][:50]})")

        broken = admin.put(f"/api/uploads/{upload_id}?offset={part}", content=payload[part : part * 2],
                           headers={"x-part-sha256": "0" * 64})
        check("часть с неверной контрольной суммой отклоняется (409)", broken.status_code == 409)

        # Обрыв связи: клиент начинает то же самое заново и узнаёт, с чего продолжать
        resumed = admin.post("/api/uploads", json=body).json()
        check("после обрыва загрузка продолжается, а не начинается заново",
              resumed["id"] == upload_id and resumed["received"] == part, f"(принято {resumed['received']})")
        check("незавершённая загрузка видна в списке", [u["id"] for u in admin.get("/api/uploads").json()] == [upload_id])

        offset = resumed["received"]
        while offset < len(payload):
            chunk = payload[offset : offset + part]
            response = send(admin, upload_id, chunk, offset)
            offset += len(chunk)
        check("все части приняты", response.json()["received"] == len(payload))
        check("принятый файл на диске лежит во временной папке",
              [p.name for p in uploads_dir.glob("*.part")] == [f"{upload_id}.part"])

        premature = admin.post("/api/uploads", json={**body, "name": "Другой.svs"}).json()
        check("незаконченный файл нельзя завершить (409)",
              admin.post(f"/api/uploads/{premature['id']}/complete").status_code == 409)
        admin.delete(f"/api/uploads/{premature['id']}")
        # 404: повтор не поможет, страница останавливает загрузку с ошибкой (ЗГ-1)
        check("часть удалённой загрузки отвечает 404",
              send(admin, premature["id"], b"x", 0).status_code == 404)

        done = admin.post(f"/api/uploads/{upload_id}/complete")
        check("загрузка завершена", done.status_code == 200, f"({done.text[:80]})")
        slide_id = done.json()["slide_id"]

        # ---------- что получилось ----------
        stored = files_on_disk(storage_root)
        check("файл лежит в хранилище под внутренним идентификатором",
              [p.relative_to(storage_root).as_posix() for p in stored] == [f"{slide_id[:2]}/{slide_id}.svs"])
        check("во временной папке ничего не осталось", not list(uploads_dir.glob("*.part")))
        check("записи о загрузке нет", db.query_one("SELECT count(*) AS n FROM uploads")["n"] == 0)
        check("на диске скан занимает ровно один файл, не копию",
              stored[0].stat().st_size == len(payload))

        catalog = admin.get("/api/catalog").json()
        card = next(s for s in catalog["slides"] if s["id"] == slide_id)
        check("скан в каталоге получил нейтральное название", card["title"] == "Скан 01", f"({card['title']})")
        check("размер и увеличение прочитаны из файла",
              (card["width"], card["height"], card["objective"]) == (512, 512, 20.0))
        check("этикетки в файле нет", card["has_label"] is False)
        check("администратор видит исходное имя файла", card.get("original_name") == SECRET_NAME)
        check("свободное место показано", catalog["space"]["free_bytes"] > 0)

        info = admin.get(f"/api/slides/{slide_id}").json()
        levels = info["width"]  # 512 px: старший уровень DeepZoom 9
        tile = admin.get(f"{info['tiles']['url']}9/0_0.jpg")
        check("тайл загруженного скана отдаётся как JPEG",
              tile.status_code == 200 and tile.content[:2] == b"\xff\xd8", f"({levels} px)")
        check("миниатюра строится", admin.get(f"/api/slides/{slide_id}/thumbnail.jpg").status_code == 200)
        check("у скана без этикетки её запрос даёт 404", admin.get(f"/api/slides/{slide_id}/label.jpg").status_code == 404)

        # Новая папка закрыта для пользователей: загруженный скан им не виден
        check("загруженный скан пользователю не виден", alice.get(f"/api/slides/{slide_id}").status_code == 404)
        admin.post("/api/access", json={"folder_id": folder, "mode": "all"})
        seen = alice.get(f"/api/slides/{slide_id}").json()
        check("после выдачи доступа виден", "title" in seen)
        check("пользователь не видит исходное имя файла", "original_name" not in seen and "Иванов" not in str(seen))

        journal = str(admin.get("/api/journal").json())
        check("в журнале есть загрузка", "slide.upload" in journal)
        check("в журнале нет исходного имени файла", "Иванов" not in journal)

        # ---------- второй скан: нумерация названий и многоуровневый файл ----------
        second_payload = build_svs(
            WORK_DIR / "second.svs", size=1024, objective=40, mpp=0.25, with_label=True
        ).read_bytes()
        state2 = admin.post("/api/uploads", json={"folder_id": folder, "name": "x.svs", "size": len(second_payload)}).json()
        send(admin, state2["id"], second_payload, 0)
        second_id = admin.post(f"/api/uploads/{state2['id']}/complete").json()["slide_id"]
        cards = {s["id"]: s for s in admin.get("/api/catalog").json()["slides"]}
        check("второй скан получил следующее название", cards[second_id]["title"] == "Скан 02",
              f"({cards[second_id]['title']})")
        check("увеличение второго скана прочитано отдельно", cards[second_id]["objective"] == 40.0)
        second_info = admin.get(f"/api/slides/{second_id}").json()
        check("этикетка второго скана найдена", second_info["has_label"] is True)
        label = admin.get(f"/api/slides/{second_id}/label.jpg")
        check("этикетка отдаётся и не кэшируется",
              label.status_code == 200 and label.content[:2] == b"\xff\xd8"
              and "no-store" in label.headers.get("cache-control", ""))
        # 1024 px: верхний уровень DeepZoom это 10, уровень 8 берётся с половинного слоя файла
        for level, name in ((10, "полное разрешение"), (8, "уменьшенный уровень")):
            response = admin.get(f"{second_info['tiles']['url']}{level}/0_0.jpg")
            check(f"тайл многоуровневого скана, {name}",
                  response.status_code == 200 and response.content[:2] == b"\xff\xd8", f"(уровень {level})")

        # ---------- файл, который не скан ----------
        junk = "это не скан, а просто текст".encode("utf-8") * 50
        state3 = admin.post("/api/uploads", json={"folder_id": folder, "name": "junk.svs", "size": len(junk)}).json()
        send(admin, state3["id"], junk, 0)
        bad = admin.post(f"/api/uploads/{state3['id']}/complete")
        check("повреждённый файл отклоняется с понятной причиной",
              bad.status_code == 400 and "скан" in bad.json()["detail"], f"({bad.json()['detail'][:60]})")
        check("он не попал в каталог", len(admin.get("/api/catalog").json()["slides"]) == 2)
        check("и не остался на диске", len(files_on_disk(storage_root)) == 2)
        check("загрузка не висит в списке", admin.get("/api/uploads").json() == [])

        # ---------- отмена ----------
        state4 = admin.post("/api/uploads", json={"folder_id": folder, "name": "c.svs", "size": 1000}).json()
        send(admin, state4["id"], b"x" * 500, 0)
        admin.delete(f"/api/uploads/{state4['id']}")
        check("отменённая загрузка стирает временный файл", not list(uploads_dir.glob("*.part")))

        # ---------- прерванная загрузка: видна и продолжается ----------
        half = payload[: len(payload) // 2]
        state5 = admin.post("/api/uploads", json={"folder_id": folder, "name": "resume.svs", "size": len(payload)}).json()
        send(admin, state5["id"], half, 0)
        listed = admin.get("/api/uploads").json()
        check("прерванная загрузка видна в списке",
              len(listed) == 1 and listed[0]["received"] == len(half),
              f"({listed[0]['received']} из {listed[0]['size']})" if listed else "")
        check("в списке есть имя файла и папка, чтобы предложить продолжить",
              listed[0]["original_name"] == "resume.svs" and listed[0]["folder_id"] == folder)
        again = admin.post("/api/uploads", json={"folder_id": folder, "name": "resume.svs", "size": len(payload)}).json()
        check("повторный выбор того же файла продолжает, а не начинает заново",
              again["id"] == state5["id"] and again["received"] == len(half))
        send(admin, state5["id"], payload[len(half):], len(half))
        resumed_id = admin.post(f"/api/uploads/{state5['id']}/complete").json()["slide_id"]
        check("продолженная загрузка завершается", bool(resumed_id))
        admin.delete(f"/api/slides/{resumed_id}")

        # ---------- другой формат: обычный пирамидальный TIFF ----------
        tif_payload = build_svs(WORK_DIR / "generic.tif", size=1024, aperio=False).read_bytes()
        state_tif = admin.post("/api/uploads", json={
            "folder_id": folder, "name": "P7_S2_PAS.TIF", "size": len(tif_payload)}).json()
        send(admin, state_tif["id"], tif_payload, 0)
        done_tif = admin.post(f"/api/uploads/{state_tif['id']}/complete")
        check("TIFF принимается", done_tif.status_code == 200, f"({done_tif.text[:80]})")
        tif_id = done_tif.json().get("slide_id", "")
        tif_key = db.query_one("SELECT key FROM slides WHERE id = ?", (tif_id,))["key"]
        check("формат виден в карточке, имя файла — нет",
              {s["id"]: s for s in admin.get("/api/catalog").json()["slides"]}[tif_id].get("format") == "TIF")
        check("файл хранится со своим расширением", tif_key.endswith(".tif") and (storage_root / tif_key).is_file(),
              f"({tif_key})")
        tif_info = admin.get(f"/api/slides/{tif_id}").json()
        check("поля карточки разобраны из имени и у TIFF", tif_info.get("stain") == "PAS", f"({tif_info.get('stain')})")
        tile = admin.get(f"{tif_info['tiles']['url']}10/0_0.jpg")
        check("тайл TIFF отдаётся", tile.status_code == 200 and tile.content[:2] == b"\xff\xd8")
        admin.delete(f"/api/slides/{tif_id}")
        check("удаление стирает и файл TIFF", not (storage_root / tif_key).exists())

        # ---------- уборка брошенных ----------
        state6 = admin.post("/api/uploads", json={"folder_id": folder, "name": "stale.svs", "size": 4000}).json()
        send(admin, state6["id"], b"y" * 2000, 0)
        stale_file = uploads_dir / f"{state6['id']}.part"
        old = time.time() - (STALE_DAYS + 1) * 86400
        os.utime(stale_file, (old, old))
        orphan = uploads_dir / "beefbeefbeefbeef.part"  # файл без записи в базе
        orphan.write_bytes(b"z" * 1000)
        result = admin.post("/api/storage/check").json()["uploads"]
        check("брошенная загрузка убрана", result["removed"] == 1, f"({result})")
        check("файл без записи в базе тоже убран", result["orphan_files"] == 1)
        check("временная папка пуста", not list(uploads_dir.glob("*.part")))
        check("свежая загрузка уборкой не тронута", admin.get("/api/uploads").json() == [])

        # ---------- удаление: файл освобождает место ----------
        admin.get(f"{info['tiles']['url']}9/0_0.jpg")  # слайд открыт в пуле: на Windows файл занят
        check("администратор удаляет скан", admin.delete(f"/api/slides/{slide_id}").status_code == 200)
        check("файл стёрт с диска", len(files_on_disk(storage_root)) == 1)
        check("скан из каталога исчез", alice.get(f"/api/slides/{slide_id}").status_code == 404)

    # ---------- нехватка места ----------
    storage = LocalFolderStorage(StorageConfig(root=str(WORK_DIR / "space"), reserve_gb=2, max_upload_gb=4))
    storage.space = lambda: DiskSpace(30 * 10**9, 3 * 10**9)  # всего 30 ГБ, свободно 3 ГБ
    try:
        storage.check_can_accept(2 * 10**9)
        check("при нехватке места загрузка отклоняется", False)
    except NotEnoughSpace as exc:
        check("при нехватке места загрузка отклоняется и называет, сколько освободить",
              "1.0 ГБ" in str(exc), f"({exc})")
    try:
        storage.check_can_accept(10**9)
        check("файл, который помещается с резервом, принимается", True)
    except NotEnoughSpace:
        check("файл, который помещается с резервом, принимается", False)


if __name__ == "__main__":
    try:
        main()
    finally:
        shutil.rmtree(WORK_DIR, ignore_errors=True)
    if failures:
        print(f"\nНе пройдено: {len(failures)}")
        for name in failures:
            print(f"  - {name}")
        raise SystemExit(1)
    print("\nВсе проверки загрузки пройдены")
