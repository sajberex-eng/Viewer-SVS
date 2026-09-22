FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Чтение KFB (KFBio): только KFB-часть ASlide; коммит и контрольные суммы файлов
# закреплены в tools/install_aslide_kfb.py (server/kfb.py). Закрытые библиотеки
# KFBio ищут свои зависимости (libcurl, libjpeg) рядом с собой.
COPY tools/install_aslide_kfb.py /tmp/
RUN pip install --no-cache-dir "numpy<2" \
    && python /tmp/install_aslide_kfb.py /usr/local/lib/python3.12/site-packages \
    && rm /tmp/install_aslide_kfb.py
ENV LD_LIBRARY_PATH=/usr/local/lib/python3.12/site-packages/Aslide/kfb/lib

COPY server ./server
COPY web ./web

# Сервис работает без прав root; каталог данных монтируется томом.
RUN useradd --create-home viewer && mkdir -p /app/data && chown viewer /app/data
USER viewer

ENV VIEWER_CONFIG=/app/config.yaml
# Скомпилированный Numba код разделения клеток (server/ij_watershed.py) хранится
# в томе данных: после пересборки образа первая оценка клеточности компилирует его
# заново (~15 с), дальше берётся готовый.
ENV NUMBA_CACHE_DIR=/app/data/cache/numba
EXPOSE 8000
CMD ["uvicorn", "server.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
