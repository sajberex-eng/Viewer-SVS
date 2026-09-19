"""Ставит в образ только KFB-часть ASlide (github.com/MrPeterJin/ASlide, GPL-3.0).

Полный пакет — 300 МБ, почти всё в нём закрытые библиотеки других форматов
(SDPC, OpenCV), которые сервису не нужны. Отсюда берутся каркас пакета
(Aslide/*.py: импорт Aslide.kfb выполняет Aslide/__init__.py), папка Aslide/kfb
с закрытыми библиотеками KFBio и лицензия — 2,4 МБ. Остальные форматы ASlide
подгружает лениво, поэтому без них пакет импортируется.

Список файлов и их SHA-256 закреплены здесь для коммита COMMIT: сборка не
меняется сама, а подменённый файл (в том числе закрытая .so) не пройдёт
проверку. Файлы качаются с raw.githubusercontent.com: он открывается с
сервера (github.com — нет) и не расходует лимит GitHub API (60 запросов в час).

Обновление ASlide: сменить COMMIT, заново снять список и суммы, проверить KFB.
Запуск (в Dockerfile): python tools/install_aslide_kfb.py <папка site-packages>
"""
from __future__ import annotations

import hashlib
import sys
import time
import urllib.request
from pathlib import Path

COMMIT = "ab25430af4659b3a843a416282b2909f9aa95604"
RAW = f"https://raw.githubusercontent.com/MrPeterJin/ASlide/{COMMIT}"
ATTEMPTS = 6

# путь в репозитории ASlide -> SHA-256
FILES = {
    "LICENSE": "3972dc9744f6499f0f9b2dbf76696f2ae7ad8af9b23dde66d6af86c9dfb36986",
    "Aslide/__init__.py": "6d240e59e51fd9c553960c8ca9f60d6d8d48578a469623738ffaf3041ee48834",
    "Aslide/aslide.py": "13612befa83d1611d212779915e3fb202bae8ed7bc576ff8eb76156332eb9578",
    "Aslide/backend_base.py": "88ac909b1000122d2cae8dfe14c745e312341de319fd79f07a400217d71f993b",
    "Aslide/bootstrap.py": "9d7a0bccf65eb9ea183f0a5191f0ad382c303af33a8dd729073f135c7bf36a5f",
    "Aslide/capabilities.py": "a262fdc53c98d17c40e9b845775bd27cb6f0072fd3f378cd055ba1cda86167a6",
    "Aslide/deepzoom.py": "3d4dd90534d43afa325267fc27cc422b623ecfb048ca17e1a068ceb9480b3e70",
    "Aslide/errors.py": "79b72f810508aacd63ce09c1516f26cba93002bbbeba20c80ef0562a5494d05d",
    "Aslide/hdf5_family.py": "dcaeb01e0c4109ff0a2e25b459bc66ec66095a6a65c2ad51e081afdc0ed391d7",
    "Aslide/registry.py": "a791631f04bfb96c1edcb1699ed4e500792708e4bf429c7b4baa04a9de4f98ba",
    "Aslide/kfb/__init__.py": "cf6dd1fcefdaf7470d01472797fc9559f63911254c9efea0b952a04e9953d81a",
    "Aslide/kfb/color_correction.py": "2c2160560bed57183f9892563e49751f6c3f9ca9ee2e4375d94d1710d1794080",
    "Aslide/kfb/icc/__init__.py": "c1d44d67a1979a84a2fe99467d24236a9e078e4f54a7ddccbb1bf91e2e959fb3",
    "Aslide/kfb/icc/real.icm": "9bfd81c10dd2ecdb87e2e1d5885f09591024244f68b7dc8e94776ba9fc206b29",
    "Aslide/kfb/kfb_deepzoom.py": "b99cabac3edf62fd25d4b597c2db6a9056e6348817896ce4f110c27b7daa1dc5",
    "Aslide/kfb/kfb_lowlevel.py": "864f17306cec7b6581e096797923e13b3f3929fef50d6ff9519ef13fcd44f6d3",
    "Aslide/kfb/kfb_slide.py": "aae30321784573d9754f01c9273894cefd6fd08dfc31bf8e42b871fa2bf2d1bf",
    "Aslide/kfb/lib/libImageOperationLib.so": "3458a8cc3aee7b58fc1d5d269b53f757a41e00e804e456ad3aadc1af89f4d4b8",
    "Aslide/kfb/lib/libcurl.so": "1a2b8b91ebb30de5d7d939bd139356fc5159b4fc46e0cf53f65608646a428a4b",
    "Aslide/kfb/lib/libjpeg.so.9": "7776a597f13735d23974f9da28b64fd6bad0b37ac8473cdff0e92dbcb6a57645",
    "Aslide/kfb/lib/libkfbslide.so": "6ca6c93e18f42bcf69746776fff6146eb21f72190aa76bdca227a24e7ad895bb",
}


def fetch(path: str, expected: str) -> bytes:
    for attempt in range(1, ATTEMPTS + 1):
        try:
            with urllib.request.urlopen(f"{RAW}/{path}", timeout=120) as response:
                data = response.read()
            if hashlib.sha256(data).hexdigest() == expected:
                return data
            print(f"  {path}: сумма не совпала (обрыв или подмена), повтор {attempt}")
        except Exception as exc:  # обрыв связи, тайм-аут, 5xx
            print(f"  {path}: {exc}, повтор {attempt}")
        time.sleep(3 * attempt)
    sys.exit(f"{path}: не удалось скачать файл с верной контрольной суммой")


def main() -> None:
    target = Path(sys.argv[1])
    for path, digest in FILES.items():
        destination = target / "Aslide" / "LICENSE" if path == "LICENSE" else target / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(fetch(path, digest))
    # libImageOperationLib.so ищет libcurl.so.4, а в ASlide она лежит как libcurl.so
    lib = target / "Aslide" / "kfb" / "lib"
    (lib / "libcurl.so.4").symlink_to("libcurl.so")
    print(f"ASlide {COMMIT[:7]}: установлено файлов {len(FILES)}, суммы совпали")


if __name__ == "__main__":
    main()
