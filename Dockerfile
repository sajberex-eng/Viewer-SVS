FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server ./server
COPY web ./web

# Сервис работает без прав root; каталог данных монтируется томом.
RUN useradd --create-home viewer && mkdir -p /app/data && chown viewer /app/data
USER viewer

ENV VIEWER_CONFIG=/app/config.yaml
EXPOSE 8000
CMD ["uvicorn", "server.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
