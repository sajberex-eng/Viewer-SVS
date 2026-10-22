r"""Стенд для проверки в браузере без рабочих данных.

    .venv\Scripts\python.exe -X utf8 tools\stand.py seed        # шесть сканов 2048 px в одном случае
    .venv\Scripts\python.exe -X utf8 tools\stand.py seed-many   # 12 случаев, подпапки, 135 сканов 512 px
    .venv\Scripts\python.exe -X utf8 tools\stand.py serve       # http://127.0.0.1:8012, stand / stand-1234

Данные в %LOCALAPPDATA%\Temp\hcd-stand (короткий путь: кэш тайлов упирается в 260 знаков).
"""
import hashlib, os, sys, warnings
from pathlib import Path
warnings.filterwarnings("ignore")
ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]
os.chdir(ROOT)
W = Path(os.environ["LOCALAPPDATA"]) / "Temp" / "hcd-stand"
W.mkdir(parents=True, exist_ok=True)
(W / "config.yaml").write_text(
    f"storage:\n  root: {(W / 'slides').as_posix()!r}\ndata_dir: {(W / 'data').as_posix()!r}\n"
    "auth:\n  https_only: false\ncellularity:\n  auto: false\n", encoding="utf-8")
os.environ["VIEWER_CONFIG"] = str(W / "config.yaml")
os.environ["VIEWER_SECRET_KEY"] = "stand-secret"
from server.main import app

def seed():
    from fastapi.testclient import TestClient
    from server.auth import hash_password
    from synthetic_svs import build_svs
    db = app.state.db
    for login, role in (("stand", "admin"), ("viewer-only", "user")):
        if not db.query_one("SELECT 1 FROM users WHERE login=?", (login,)):
            db.execute("INSERT INTO users (login, password_hash, role, name) VALUES (?,?,?,?)",
                       (login, hash_password("stand-1234"), role, login))
    with TestClient(app, base_url="http://127.0.0.1") as c:
        assert c.post("/api/login", json={"login": "stand", "password": "stand-1234"}).status_code == 200
        folder = c.post("/api/folders", json={"name": "Случай 1917"}).json()["id"]
        specs = [("HE", None, 40), ("IHC", "BCL-2", 40), ("IHC", "CD-3", 40), ("HE", None, 20), ("IHC", "Ki-67", 40), ("IHC", "CD10", 40)]
        for i, (stain, marker, obj) in enumerate(specs):
            p = build_svs(W / f"s{i}.svs", size=2048, objective=obj, mpp=0.25 if obj == 40 else 0.5)
            data = p.read_bytes()
            st = c.post("/api/uploads", json={"folder_id": folder, "name": f"scan-{i}.svs", "size": len(data)}).json()
            c.put(f"/api/uploads/{st['id']}?offset=0", content=data, headers={"x-part-sha256": hashlib.sha256(data).hexdigest()})
            sid = c.post(f"/api/uploads/{st['id']}/complete").json()["slide_id"]
            c.patch(f"/api/slides/{sid}", json={"stain": stain, "ihc_marker": marker})
    print("seeded")

def serve():
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8012, log_level="warning")

COMMANDS = {"seed": seed, "serve": serve}
if len(sys.argv) < 2 or sys.argv[1] not in (*COMMANDS, "seed-many"):
    sys.exit("Использование: stand.py seed | seed-many | serve")
if sys.argv[1] in COMMANDS:
    COMMANDS[sys.argv[1]]()

# Много случаев для проверки каталога (этап 15): 12 случаев, у части подпапки,
# по 3–8 маленьких сканов, у «Случай 7» — 70; даты разнесены по дням.
def seed_many():
    import random, datetime
    from fastapi.testclient import TestClient
    from synthetic_svs import build_svs
    random.seed(15)
    db = app.state.db
    markers = ["CD20", "CD3", "CD5", "CD10", "Ki-67", "BCL-2", "BCL-6", "CD30", "PD-1", "kappa", "lambda", "MUM1"]
    stains = ["HE", "HE", "HE", "AE", "GOMORI", "CONGO", "IHC", "IHC", "IHC"]
    small = build_svs(W / "small.svs", size=512, objective=20, mpp=0.5).read_bytes()
    digest = hashlib.sha256(small).hexdigest()
    day = 0
    with TestClient(app, base_url="http://127.0.0.1") as c:
        assert c.post("/api/login", json={"login": "stand", "password": "stand-1234"}).status_code == 200
        for n in range(1, 13):
            case = c.post("/api/folders", json={"name": f"Случай {1000 + n}"}).json()["id"]
            targets = [case]
            if n % 4 == 0:
                for sub in ("Биопсия", "Трепан"):
                    targets.append(c.post("/api/folders", json={"name": sub, "parent_id": case}).json()["id"])
            count = 70 if n == 7 else random.randint(3, 8)
            for i in range(count):
                folder = random.choice(targets)
                st = c.post("/api/uploads", json={"folder_id": folder, "name": f"c{n}-{i}.svs", "size": len(small)}).json()
                c.put(f"/api/uploads/{st['id']}?offset=0", content=small, headers={"x-part-sha256": digest})
                sid = c.post(f"/api/uploads/{st['id']}/complete").json()["slide_id"]
                stain = random.choice(stains)
                marker = random.choice(markers) if stain == "IHC" else None
                c.patch(f"/api/slides/{sid}", json={"stain": stain, "ihc_marker": marker, "note": "контроль" if i % 5 == 0 else ""})
                day += 1
                when = (datetime.datetime(2026, 6, 1) + datetime.timedelta(hours=day * 7)).strftime("%Y-%m-%d %H:%M:%S")
                db.execute("UPDATE slides SET added_at = ? WHERE id = ?", (when, sid))
    print("seeded many")

if len(sys.argv) > 1 and sys.argv[1] == "seed-many":
    seed_many()
