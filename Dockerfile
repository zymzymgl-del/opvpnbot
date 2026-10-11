# OPVPN — образ для запуска в Docker.
# Сборка:  docker build -t opvpn .
# Запуск:  docker compose up -d --build
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# tini — корректная обработка сигналов/зонтичный процесс (важно для graceful shutdown),
# ca-certificates — для HTTPS-запросов к API ЮKassa/ЮMoney и панелям.
RUN apt-get update \
 && apt-get install -y --no-install-recommends tini ca-certificates \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Непривилегированный пользователь; каталог данных заранее создаём и отдаём ему.
RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin app \
 && mkdir -p /app/data /app/data/backups \
 && chown -R app:app /app

USER app

# База, логи и бэкапы живут здесь (том монтируется в этот каталог).
VOLUME ["/app/data"]

# Порт HTTP-сервера вебхуков ЮKassa (используется при WEBHOOK_ENABLED=true).
EXPOSE 8080

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-u", "bot.py"]
