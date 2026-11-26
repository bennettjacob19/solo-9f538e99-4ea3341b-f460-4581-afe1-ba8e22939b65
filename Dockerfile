FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    DB_PATH=/data/drill.db

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY wsgi.py ./wsgi.py

RUN mkdir -p /data \
    && useradd --system --uid 10001 drill \
    && chown -R drill:drill /app /data
USER drill

VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=10s --timeout=4s --start-period=5s --retries=5 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3)" || exit 1

# One worker + threads: the in-process publish lock stays meaningful while
# replay requests are still served during the simulated version switch.
CMD ["gunicorn", "--workers", "1", "--threads", "8", \
     "--bind", "0.0.0.0:8000", "--access-logfile", "-", "wsgi:app"]
