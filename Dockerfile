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

# Copy application source code and entrypoint
COPY secops_ingestion/ ./secops_ingestion/
COPY static/ ./static/
COPY templates/ ./templates/
COPY app.py .
COPY secops_ingestion_cli.py .
COPY docker-entrypoint.sh .
RUN chmod +x docker-entrypoint.sh

ENV PORT=443 \
    PYTHONUNBUFFERED=1 \
    MOCK_MODE=false \
    SSL_ENABLED=true

EXPOSE 443 5000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD curl -k -f https://localhost:${PORT}/api/status || curl -f http://localhost:${PORT}/api/status || exit 1

ENTRYPOINT ["/app/docker-entrypoint.sh"]
