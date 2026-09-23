"""
SecOps Ingestion Dashboard - Flask Application
Serves interactive metrics, charts, 30m reconciliation, and BigQuery migration views.
"""
import os
import io
import csv
import json
from datetime import datetime, timedelta, timezone
from typing import Dict, Any

from flask import Flask, render_template, request, jsonify, Response, send_file

from secops_ingestion.config import (
    DEFAULT_PROJECT_ID,
    ROLLUP_30M_SECONDS,
    METRIC_BYTES_COUNT,
    METRIC_RECORD_COUNT,
    METRIC_NORMALIZER_EVENT_COUNT,
    PERIOD_CONFIGS,
)
from secops_ingestion.client import SecOpsMonitoringClient
from secops_ingestion.calculator import IngestionCalculator

app = Flask(__name__)
app.config["SECOPS_PROJECT_ID"] = os.environ.get("SECOPS_PROJECT_ID", DEFAULT_PROJECT_ID)
app.config["MOCK_MODE"] = os.environ.get("MOCK_MODE", "false").lower() in ("true", "1", "yes")
app.config["CREDENTIALS_PATH"] = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
app.config["SSL_ENABLED"] = os.environ.get("SSL_ENABLED", "false").lower() in ("true", "1", "yes")
app.config["SSL_CERT_PATH"] = os.environ.get("SSL_CERT_PATH", "certs/cert.pem")
app.config["SSL_KEY_PATH"] = os.environ.get("SSL_KEY_PATH", "certs/key.pem")

# In-memory cache for fast UI interaction
_cache: Dict[str, Any] = {}


def get_client() -> SecOpsMonitoringClient:
    """Instantiate a client configured from app config."""
    return SecOpsMonitoringClient(
        project_id=app.config["SECOPS_PROJECT_ID"],
        credentials_path=app.config.get("CREDENTIALS_PATH"),
        mock_mode=app.config.get("MOCK_MODE", False),
    )


def fetch_period_summary(period_key: str):
    """Fetches and calculates summary for 'daily', 'weekly', 'monthly', or 'yearly' (12 months)."""
    cache_key = f"{app.config['SECOPS_PROJECT_ID']}_{period_key}_{app.config.get('MOCK_MODE', False)}"
    cached = _cache.get(cache_key)
    now = datetime.now(timezone.utc)

    # Cache valid for 3 minutes
    if cached and (now - cached["timestamp"]).total_seconds() < 180:
        return cached["summary"]

    client = get_client()
    days, alignment, title = PERIOD_CONFIGS.get(
        period_key,
        (1, ROLLUP_30M_SECONDS, "Daily (Last 24 Hours)")
    )

    start_time = now - timedelta(days=days)
    end_time = now

    raw_metrics = client.fetch_all_metrics(
        start_time=start_time,
        end_time=end_time,
        alignment_period_seconds=alignment,
    )
    summary = IngestionCalculator.calculate_summary(
        metrics_dict=raw_metrics,
        period_name=title,
        start_time=start_time,
        end_time=end_time,
    )

    _cache[cache_key] = {
        "timestamp": now,
        "summary": summary,
    }
    return summary


@app.route("/")
def index():
    """Main dashboard page."""
    client = get_client()
    auth_info = client.get_auth_status()
    ssl_active = bool(request.is_secure or app.config.get("SSL_ENABLED", False))
    return render_template(
        "index.html",
        project_id=app.config["SECOPS_PROJECT_ID"],
        mock_mode=app.config.get("MOCK_MODE", False),
        auth_info=auth_info,
        ssl_enabled=ssl_active,
    )


@app.route("/api/status")
def api_status():
    """Returns current project, auth mode, and connection details."""
    client = get_client()
    status = client.get_auth_status()
    status["cached_entries"] = len(_cache)
    status["ssl"] = bool(request.is_secure or app.config.get("SSL_ENABLED", False))
    cert_path = app.config.get("SSL_CERT_PATH")
    if cert_path and os.path.exists(cert_path):
        try:
            from secops_ingestion.ssl_util import get_certificate_info
            status["ssl_certificate"] = get_certificate_info(cert_path)
        except Exception:
            pass
    return jsonify(status)


@app.route("/api/settings", methods=["POST"])
def api_settings():
    """Updates settings like project_id, mock_mode, or credentials."""
    data = request.json or {}
    if "project_id" in data and data["project_id"].strip():
        app.config["SECOPS_PROJECT_ID"] = data["project_id"].strip()
    if "mock_mode" in data:
        app.config["MOCK_MODE"] = bool(data["mock_mode"])
    if "credentials_path" in data:
        app.config["CREDENTIALS_PATH"] = data["credentials_path"].strip() or None

    _cache.clear()
    return jsonify({
        "success": True,
        "project_id": app.config["SECOPS_PROJECT_ID"],
        "mock_mode": app.config["MOCK_MODE"],
        "credentials_path": app.config["CREDENTIALS_PATH"],
    })


@app.route("/api/refresh", methods=["POST"])
def api_refresh():
    """Clears cached metrics and forces reload."""
    _cache.clear()
    return jsonify({"success": True, "message": "Cache invalidated."})


@app.route("/api/ingestion/summary")
def api_summary():
    """Returns overall ingestion summary for selected timeframe."""
    period = request.args.get("period", "daily").lower()
    summary = fetch_period_summary(period)

    return jsonify({
        "period": summary.period_name,
        "start_time": summary.start_time,
        "end_time": summary.end_time,
        "total_bytes": summary.total_bytes,
        "total_size_mb": summary.total_size_mb,
        "total_size_gb": summary.total_size_gb,
        "total_records": summary.total_records,
        "total_normalized_events": summary.total_normalized_events,
        "overall_norm_ratio": summary.overall_norm_ratio,
        "active_log_types_count": summary.active_log_types_count,
        "healthy_sources_count": summary.healthy_sources_count,
        "warning_sources_count": summary.warning_sources_count,
        "critical_sources_count": summary.critical_sources_count,
    })


@app.route("/api/ingestion/breakdown")
def api_breakdown():
    """Returns log sources breakdown list."""
    period = request.args.get("period", "daily").lower()
    summary = fetch_period_summary(period)

    items = []
    for src in summary.log_types:
        items.append({
            "log_type": src.log_type,
            "collectors": src.collectors,
            "collector_count": len(src.collectors),
            "bytes_count": src.bytes_count,
            "size_mb": src.size_mb,
            "size_gb": src.size_gb,
            "record_count": src.record_count,
            "normalized_event_count": src.normalized_event_count,
            "error_events": src.error_events,
            "normalization_rate_pct": src.normalization_rate_pct,
            "avg_bytes_per_record": src.avg_bytes_per_record,
            "health_status": src.health_status,
            "health_color": src.health_color,
            "recommendation": src.recommendation,
        })

    return jsonify({
        "period": summary.period_name,
        "log_types": items,
    })


@app.route("/api/ingestion/timeseries")
def api_timeseries():
    """Returns chronological data points for Chart.js visualization."""
    period = request.args.get("period", "daily").lower()
    summary = fetch_period_summary(period)
    return jsonify({
        "period": summary.period_name,
        "timeline": summary.timeline_series,
    })


@app.route("/api/ingestion/reconcile")
def api_reconcile():
    """Runs customer 30-minute sum vs rollup window reconciliation check."""
    client = get_client()
    period = request.args.get("period", "daily").lower()
    days = PERIOD_CONFIGS.get(period, (1, ROLLUP_30M_SECONDS, "Daily"))[0]
    now = datetime.now(timezone.utc)
    start_time = now - timedelta(days=days)

    reconcile_data = client.reconcile_30m_vs_rollup(
        metric_type=METRIC_BYTES_COUNT,
        start_time=start_time,
        end_time=now,
    )
    return jsonify({
        "period": period,
        "reconciliation": reconcile_data,
        "total_30m_sum_bytes": sum(r["sum_30m"] for r in reconcile_data),
        "total_rollup_bytes": sum(r["rollup"] for r in reconcile_data),
        "total_official_max_bytes": sum(r["official_max"] for r in reconcile_data),
    })


@app.route("/api/ingestion/bigquery-compat")
def api_bigquery_compat():
    """Returns data formatted according to the legacy BigQuery datalake.ingestion_metrics schema."""
    period = request.args.get("period", "daily").lower()
    summary = fetch_period_summary(period)
    bq_rows = IngestionCalculator.to_bigquery_compat_table(summary)
    return jsonify({
        "table_name": f"chronicle-{app.config['SECOPS_PROJECT_ID']}.datalake.ingestion_metrics",
        "migrated_from": "BigQuery (May 2025)",
        "replacement_api": "Cloud Monitoring API v3 (projects.timeSeries.list)",
        "rows": bq_rows,
    })


@app.route("/api/export/csv")
def api_export_csv():
    """Generates and downloads CSV of the active summary."""
    period = request.args.get("period", "daily").lower()
    summary = fetch_period_summary(period)

    output = io.StringIO()
    fieldnames = [
        "log_type",
        "collector_ids",
        "size_mb",
        "size_gb",
        "record_count",
        "normalized_event_count",
        "error_events",
        "normalization_rate_pct",
        "avg_bytes_per_record",
        "health_status",
    ]
    writer = csv.DictWriter(output, fieldnames=fieldnames)
    writer.writeheader()
    for src in summary.log_types:
        writer.writerow({
            "log_type": src.log_type,
            "collector_ids": ":RQ:".join(src.collectors),
            "size_mb": src.size_mb,
            "size_gb": src.size_gb,
            "record_count": src.record_count,
            "normalized_event_count": src.normalized_event_count,
            "error_events": src.error_events,
            "normalization_rate_pct": src.normalization_rate_pct,
            "avg_bytes_per_record": src.avg_bytes_per_record,
            "health_status": src.health_status,
        })

    filename = f"secops_ingestion_{app.config['SECOPS_PROJECT_ID']}_{period}.csv"
    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment;filename={filename}"},
    )


@app.route("/api/export/json")
def api_export_json():
    """Generates and downloads JSON."""
    period = request.args.get("period", "daily").lower()
    summary = fetch_period_summary(period)
    filename = f"secops_ingestion_{app.config['SECOPS_PROJECT_ID']}_{period}.json"
    
    data = {
        "project_id": app.config["SECOPS_PROJECT_ID"],
        "period": summary.period_name,
        "start_time": summary.start_time,
        "end_time": summary.end_time,
        "total_bytes": summary.total_bytes,
        "total_size_mb": summary.total_size_mb,
        "total_size_gb": summary.total_size_gb,
        "total_records": summary.total_records,
        "total_normalized_events": summary.total_normalized_events,
        "overall_norm_ratio": summary.overall_norm_ratio,
        "log_types": [
            {
                "log_type": s.log_type,
                "collectors": s.collectors,
                "size_mb": s.size_mb,
                "size_gb": s.size_gb,
                "record_count": s.record_count,
                "normalized_event_count": s.normalized_event_count,
                "error_events": s.error_events,
                "normalization_rate_pct": s.normalization_rate_pct,
                "health_status": s.health_status,
            }
            for s in summary.log_types
        ],
    }
    return Response(
        json.dumps(data, indent=2),
        mimetype="application/json",
        headers={"Content-Disposition": f"attachment;filename={filename}"},
    )


if __name__ == "__main__":
    ssl_enabled = os.environ.get("SSL_ENABLED", "false").lower() in ("true", "1", "yes")
    cert_path = os.environ.get("SSL_CERT_PATH", "certs/cert.pem")
    key_path = os.environ.get("SSL_KEY_PATH", "certs/key.pem")

    default_port = 443 if (ssl_enabled or (os.path.exists(cert_path) and os.path.exists(key_path) and os.environ.get("SSL_ENABLED") != "0")) else 5000
    port = int(os.environ.get("PORT", default_port))

    ssl_context = None
    protocol = "http"
    if ssl_enabled or (os.path.exists(cert_path) and os.path.exists(key_path) and os.environ.get("SSL_ENABLED") != "0"):
        from secops_ingestion.ssl_util import ensure_ssl_credentials, create_ssl_context
        c_path, k_path = ensure_ssl_credentials(cert_path, key_path)
        ssl_context = create_ssl_context(c_path, k_path)
        protocol = "https"
        app.config["SSL_ENABLED"] = True
        app.config["SSL_CERT_PATH"] = c_path
        app.config["SSL_KEY_PATH"] = k_path
        print(f"🔒 SSL/TLS Enabled (Cert: {c_path}, Key: {k_path})")

    print(f"\n🚀 Launching SecOps Ingestion Dashboard on {protocol}://localhost:{port}")
    app.run(host="0.0.0.0", port=port, debug=False, ssl_context=ssl_context)

