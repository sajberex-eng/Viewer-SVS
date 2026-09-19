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
EXPOSE 8000
CMD ["uvicorn", "server.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
