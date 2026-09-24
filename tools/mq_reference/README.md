# Эталон MarrowQuant 2.0 в ImageJ

Сверка переноса клеточности (`server/cellularity.py`, `server/ij_watershed.py`) с
оригиналом по каждой точке каждой маски. Этап 11, раздел 13.8 ТЗ.

`MQReference.java` дословно повторяет шаги класса `MarrowQuant` из
`MarrowQuant2.0.groovy` (github.com/Naveiras-Lab/MarrowQuant2.0, BSD-3) через те же
вызовы ImageJ 1.54f и Colour Deconvolution 3.0.2 — те версии, что стоят в QuPath 0.5.1.
QuPath не нужен: область скана, ткань и артефакты приходят картинками.

Всё выполняется на сервере: сканы не покидают его, файлы в репозиторий не попадают.

## Подготовка (один раз)

```
mkdir -p /opt/mqref && cd /opt/mqref
curl -so ij.jar https://repo1.maven.org/maven2/net/imagej/ij/1.54f/ij-1.54f.jar
curl -so cd.jar https://maven.scijava.org/content/groups/public/sc/fiji/Colour_Deconvolution/3.0.2/Colour_Deconvolution-3.0.2.jar
sha1sum ij.jar cd.jar   # 42e9829a43edb136bac6794112c476fd943f53b2, 086580ff33736e78d74b46c7de6c764b4057da12
docker build -t mqref <папка tools/mq_reference>
docker run --rm -v /opt/mqref:/m -w /m/work mqref javac -cp /m/ij.jar:/m/cd.jar MQReference.java
```

Код вьювера для сверки кладётся в `/opt/mqref/code` (папки `server` и `tools`). OpenCV, Numba и NumPy
есть в образе вьювера с выкладки `aa221d6`; до неё ставились в `/opt/mqref/pkgs` (удалена 2026-09-22).

## Сверка

```
# область фрагмента: номер фрагмента (0 — самый крупный), уменьшение (4 — как в оригинале)
docker run --rm -v /opt/mqref:/m -v viewer-svs_viewer-data:/data:ro \
  viewer-svs-viewer python -X utf8 /m/code/tools/mq_reference/prepare.py /data/slides/<ключ> /m/work/f0 0 4
# ImageJ: команды с параметрами идут через окна, поэтому нужен виртуальный экран
docker run --rm -v /opt/mqref:/m -w /m/work mqref sh -c \
  "Xvfb :99 -screen 0 1280x1024x24 -nolisten tcp >/dev/null 2>&1 & sleep 2; \
   DISPLAY=:99 java -Xmx1200m -cp /m/ij.jar:/m/cd.jar:. MQReference /m/work/f0 $(cat /opt/mqref/work/f0/pixel_um.txt) 300.0 1000000000 0.3"
# наш перенос на тех же картинках и сравнение масок
docker run --rm -v /opt/mqref:/m -e NUMBA_CACHE_DIR=/tmp viewer-svs-viewer \
  python -X utf8 /m/code/tools/mq_reference/compare.py /m/work/f0 300 1000000000 0.3
```

`steps.py` проверяет шаги по отдельности: вход каждого шага берётся из ImageJ, поэтому
ошибка одного шага не прячется за ошибкой другого.

`xvfb-run` в контейнере зависает (не дожидается готовности экрана) — экран запускается вручную.
Длинные расчёты запускать через `nohup … &` с выводом в файл: SSH с компьютера заказчика рвётся.

## Итог сверки 2026-09-22

Скан 840a40f2941d (H&E, 20×), четыре фрагмента при разрешении оригинала (2,02 мкм на точку)
и крупный фрагмент при 1,01 мкм: все маски (кость, строма и сосуды, кроветворная ткань, жир,
промежуточные маски жировых кандидатов) и число жировых клеток совпали с ImageJ до точки.

## Что лежало на рабочем сервере (удалено 2026-09-24)

Эталон ставился на сервер 2026-09-22 только для сверки и в работу сервиса не входил.
После отмены сверки с QuPath заказчик 2026-09-24 разрешил всё удалить: `/opt/mqref`
(jar ImageJ и Colour Deconvolution, копии кода, области скана `work/`, маски, журналы),
образы Docker `mqref` и `eclipse-temurin:21-jdk`, кэш сборки. На сервере от эталона
ничего не осталось; повторная сверка ставится заново по разделам выше.
