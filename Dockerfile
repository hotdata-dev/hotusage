# hotusage server: ingest + admin dashboard, all state in hotdata.
FROM python:3.12-slim

RUN pip install --no-cache-dir hotdata duckdb && \
    useradd --create-home --uid 10001 appuser

WORKDIR /app
COPY core.py ./
COPY server/ server/

USER 10001

# Config via env: HOTDATA_API_KEY (required), HOTUSAGE_INGEST_TOKEN (required
# outside dev), HOTDATA_WORKSPACE / HOTDATA_API_HOST optional overrides.
EXPOSE 8377
CMD ["python3", "server/server.py", "--host", "0.0.0.0", "--port", "8377"]
