#!/bin/sh
set -e

PORT="${PORT:-443}"

# Determine whether SSL should be enabled
IS_SSL="false"
if [ "$SSL_ENABLED" = "true" ] || [ "$SSL_ENABLED" = "1" ] || [ "$PORT" = "443" ] || [ "$PORT" = "8443" ]; then
    IS_SSL="true"
fi

# Cloud Run injects K_SERVICE and handles TLS termination at the edge; container serves HTTP
if [ -n "$K_SERVICE" ] || [ "$CLOUD_RUN" = "true" ]; then
    IS_SSL="false"
fi

if [ "$IS_SSL" = "true" ]; then
    CERT_FILE="${SSL_CERT_PATH:-/app/certs/cert.pem}"
    KEY_FILE="${SSL_KEY_PATH:-/app/certs/key.pem}"

    mkdir -p "$(dirname "$CERT_FILE")"
    mkdir -p "$(dirname "$KEY_FILE")"

    if [ ! -f "$CERT_FILE" ] || [ ! -f "$KEY_FILE" ]; then
        echo "[Entrypoint] SSL enabled but certificate not found at $CERT_FILE. Generating self-signed certificate..."
        python -c "from secops_ingestion.ssl_util import ensure_ssl_credentials; ensure_ssl_credentials('$CERT_FILE', '$KEY_FILE')"
    fi

    echo "[Entrypoint] Launching Gunicorn on HTTPS 0.0.0.0:${PORT} with SSL"
    exec gunicorn --bind "0.0.0.0:${PORT}" \
        --workers 2 --threads 4 --timeout 120 \
        --certfile "$CERT_FILE" --keyfile "$KEY_FILE" \
        app:app
else
    echo "[Entrypoint] Launching Gunicorn on HTTP 0.0.0.0:${PORT}"
    exec gunicorn --bind "0.0.0.0:${PORT}" \
        --workers 2 --threads 4 --timeout 120 \
        app:app
fi
