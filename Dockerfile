FROM python:3.13-slim
LABEL org.opencontainers.image.source="https://github.com/DieLaughing/ballotfacts" \
      org.opencontainers.image.licenses="AGPL-3.0-or-later"
ARG BF_COMMIT=dev
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 BF_DATA=/data BF_COMMIT=$BF_COMMIT
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY bf ./bf
USER 1000:1000
EXPOSE 8796
HEALTHCHECK --interval=60s --timeout=5s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8796/healthz')"
# --no-access-log: we don't keep a record of who looked at what
CMD ["uvicorn", "bf.app:app", "--host", "0.0.0.0", "--port", "8796", "--no-access-log", "--proxy-headers", "--forwarded-allow-ips", "*"]
