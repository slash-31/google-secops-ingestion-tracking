FROM python:3.11-slim

WORKDIR /app

# Install runtime system packages
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Install python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source code
COPY secops_ingestion/ ./secops_ingestion/
COPY static/ ./static/
COPY templates/ ./templates/
COPY app.py .
COPY secops_ingestion_cli.py .

ENV PORT=5000 \
    PYTHONUNBUFFERED=1 \
    MOCK_MODE=false

EXPOSE 5000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD curl -f http://localhost:${PORT}/api/status || exit 1

CMD ["sh", "-c", "gunicorn --bind 0.0.0.0:${PORT} --workers 2 --threads 4 --timeout 120 app:app"]
