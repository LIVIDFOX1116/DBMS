# FrameCheck API + dashboard, CPU only: the GPU path (RAPIDS) is optional
# everywhere and lives in WSL2, and graph/backend.py falls back to
# pandas + NetworkX. Render injects PORT; locally it defaults to 8000.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Only what the running app needs. datasets/ carries the demo dataset for
# FRAMECHECK_IMPORT_DEMO=1. .dockerignore keeps secrets out of the context.
COPY api/ api/
COPY db/ db/
COPY graph/ graph/
COPY ingestion/ ingestion/
COPY frontend/ frontend/
COPY scripts/ scripts/
COPY datasets/ datasets/
COPY docker/ docker/

RUN useradd --create-home --uid 10001 framecheck \
    && mkdir -p /data/snapshots \
    && chown -R framecheck:framecheck /data /app
USER framecheck
ENV FRAMECHECK_DATA_DIR=/data

EXPOSE 8000
# Slim has no curl. Uses the same PORT the server was given.
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD python -c "import os,urllib.request as u; u.urlopen('http://127.0.0.1:'+os.getenv('PORT','8000')+'/health',timeout=4)"

# Applies pending migrations (and, if asked, the demo dataset), then serves.
CMD ["python", "docker/entrypoint.py"]
