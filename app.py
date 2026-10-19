"""
SecOps Ingestion Dashboard - Flask Application
Serves interactive metrics, charts, 30m reconciliation, and BigQuery migration views.
"""
import os
import io
import csv
import json
from datetime import datetime, timedelta, timezone
from typing import Dict, Any, Optional, Tuple

from flask import Flask, render_template, request, jsonify, Response, send_file

from secops_ingestion.config import (
    DEFAULT_PROJECT_ID,
    ROLLUP_30M_SECONDS,
    METRIC_BYTES_COUNT,
    METRIC_RECORD_COUNT,
    METRIC_NORMALIZER_EVENT_COUNT,
    PERIOD_CONFIGS,
)
from secops_ingestion.client import SecOpsMonitoringClient, MonitoringAuthError
from secops_ingestion.calculator import IngestionCalculator
from secops_ingestion.security import (
    apply_security_headers,
    require_secret,
    rate_limit,
    RateLimiter,
    validate_project_id,
    redact_project_id,
    safe_filename_component,
    csv_safe,
)
from secops_ingestion.bindplane import (
    BindPlaneClient,
    BindPlaneError,
    MockBindPlaneClient,
    ALLOWED_PERIODS as BINDPLANE_PERIODS,
)
from secops_ingestion.delivery import (
    DEFAULT_BINDPLANE_COLLECTOR_IDS,
    bindplane_bytes_by_group,
    detect_idle_sources,
    evaluate_delivery,
    map_sources,
    recent_window,
    secops_bytes_by_log_type,
)
from secops_ingestion.unified import build_unified_rows
from secops_ingestion.pipeline_health import (
    Issue,
    IssueTracker,
    Thresholds,
    SEVERITY_CRITICAL,
    count_by_severity,
    evaluate_bindplane,
    evaluate_secops_parsing,
    evaluate_secops_silence,
)

app = Flask(__name__)
app.config["SECOPS_PROJECT_ID"] = os.environ.get("SECOPS_PROJECT_ID", DEFAULT_PROJECT_ID)
app.config["MOCK_MODE"] = os.environ.get("MOCK_MODE", "false").lower() in ("true", "1", "yes")
app.config["CREDENTIALS_PATH"] = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
app.config["SSL_ENABLED"] = os.environ.get("SSL_ENABLED", "false").lower() in ("true", "1", "yes")
app.config["SSL_CERT_PATH"] = os.environ.get("SSL_CERT_PATH", "certs/cert.pem")
app.config["SSL_KEY_PATH"] = os.environ.get("SSL_KEY_PATH", "certs/key.pem")
# Opt-in only. When false, a Cloud Monitoring outage surfaces as an error rather
# than as synthetic metrics presented as live telemetry.
app.config["ALLOW_MOCK_FALLBACK"] = os.environ.get(
    "ALLOW_MOCK_FALLBACK", "false"
).lower() in ("true", "1", "yes")

app.after_request(apply_security_headers)

# Cloud Monitoring queries are the expensive path; keep a per-worker ceiling.
_collect_limiter = RateLimiter(limit=6, window_seconds=300)
_query_limiter = RateLimiter(limit=120, window_seconds=60)

# In-memory cache for fast UI interaction
_cache: Dict[str, Any] = {}

# Pipeline health: short TTL so outages surface within about a minute.
PIPELINE_CACHE_SECONDS = 60
_pipeline_cache: Dict[str, Any] = {}
# One tracker per window so switching windows doesn't flap first_seen/resolved.
_issue_trackers: Dict[str, IssueTracker] = {}


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def load_thresholds() -> Thresholds:
    """Detection thresholds, overridable via environment."""
    return Thresholds(
        silent_bytes_per_sec=_env_float("PIPELINE_SILENT_BPS", 0.0),
        drop_warn_pct=_env_float("PIPELINE_DROP_WARN_PCT", 20.0),
        drop_crit_pct=_env_float("PIPELINE_DROP_CRIT_PCT", 50.0),
        drop_min_input_bytes_per_sec=_env_float("PIPELINE_DROP_MIN_INPUT_BPS", 100.0),
        secops_silent_after_minutes=int(_env_float("SECOPS_SILENT_AFTER_MINUTES", 90)),
        secops_cadence_multiplier=_env_float("SECOPS_SILENT_CADENCE_MULTIPLIER", 3.0),
        secops_min_buckets_for_cadence=int(_env_float("SECOPS_SILENT_MIN_BUCKETS", 4)),
        secops_silent_ignore=tuple(
            s.strip() for s in os.environ.get("SECOPS_SILENT_IGNORE_LOG_TYPES", "").split(",") if s.strip()
        ),
        delivery_gap_warn_pct=_env_float("DELIVERY_GAP_WARN_PCT", 50.0),
        delivery_gap_crit_pct=_env_float("DELIVERY_GAP_CRIT_PCT", 80.0),
        delivery_min_bytes=_env_float("DELIVERY_MIN_BYTES", 1_000_000.0),
        delivery_baseline_outlier_pct=_env_float("DELIVERY_BASELINE_OUTLIER_PCT", 75.0),
    )


def bindplane_collector_ids() -> Tuple[str, ...]:
    """SecOps collector_id(s) that identify BindPlane traffic (env-overridable)."""
    raw = os.environ.get("BINDPLANE_SECOPS_COLLECTOR_IDS", "")
    ids = tuple(x.strip() for x in raw.split(",") if x.strip())
    return ids or DEFAULT_BINDPLANE_COLLECTOR_IDS


def get_bindplane_client():
    """Mock client in mock mode, otherwise env-configured client (or None)."""
    if app.config.get("MOCK_MODE", False):
        return MockBindPlaneClient()
    return BindPlaneClient.from_env()


def resolve_period(raw: str) -> str:
    """
    Map caller input to a known period key, defaulting to 'daily'.

    Keeps unvalidated query-string data out of Content-Disposition headers and
    out of cache keys.
    """
    candidate = (raw or "").strip().lower()
    return candidate if candidate in PERIOD_CONFIGS else "daily"


def get_client() -> SecOpsMonitoringClient:
    """Instantiate a client configured from app config."""
    return SecOpsMonitoringClient(
        project_id=app.config["SECOPS_PROJECT_ID"],
        credentials_path=app.config.get("CREDENTIALS_PATH"),
        mock_mode=app.config.get("MOCK_MODE", False),
        allow_mock_fallback=app.config.get("ALLOW_MOCK_FALLBACK", False),
    )


def fetch_period_summary(period_key: str, force_reload: bool = False):
    """Fetches and calculates summary for 'daily', 'weekly', 'monthly', or 'yearly' (12 months)."""
    return _load_period(period_key, force_reload)["summary"]


def fetch_period_raw(period_key: str) -> Dict[str, Any]:
    """Return the raw Cloud Monitoring series behind a period summary (cached)."""
    return _load_period(period_key)["raw"]


def _load_period(period_key: str, force_reload: bool = False) -> Dict[str, Any]:
    """Fetch, calculate, and cache {summary, raw, timestamp} for a period."""
    cache_key = f"{app.config['SECOPS_PROJECT_ID']}_{period_key}_{app.config.get('MOCK_MODE', False)}"
    cached = _cache.get(cache_key)
    now = datetime.now(timezone.utc)

    # Cache valid for 3 minutes unless force_reload is requested
    if not force_reload and cached and (now - cached["timestamp"]).total_seconds() < 180:
        return cached

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

    entry = {
        "timestamp": now,
        "summary": summary,
        "raw": raw_metrics,
    }
    _cache[cache_key] = entry
    return entry


@app.errorhandler(MonitoringAuthError)
def handle_monitoring_auth_error(exc):
    """Surface telemetry unavailability explicitly instead of faking healthy data."""
    app.logger.error("Cloud Monitoring unavailable: %s", exc)
    return jsonify({
        "error": "Telemetry unavailable",
        "detail": "Cloud Monitoring could not be queried. Metrics are NOT being "
                  "displayed because substituting synthetic data would be misleading.",
        "data_source": "unavailable",
    }), 503


@app.errorhandler(500)
def handle_internal_error(exc):
    """Generic 500 body so internals are never disclosed to a public caller."""
    app.logger.exception("Unhandled server error")
    return jsonify({"error": "Internal server error"}), 500


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
    """Returns connection health. Unauthenticated, so the payload is redacted."""
    client = get_client()
    raw = client.get_auth_status()

    status = {
        "authenticated": raw.get("authenticated", False),
        "mode": raw.get("mode", "live"),
        "project_id": redact_project_id(raw.get("project_id")),
        "credentials_source": raw.get("credentials_source", "Application Default Credentials"),
        "message": raw.get("message", ""),
        "cached_entries": len(_cache),
        "ssl": bool(request.is_secure or app.config.get("SSL_ENABLED", False)),
    }

    cert_path = app.config.get("SSL_CERT_PATH")
    if cert_path and os.path.exists(cert_path):
        try:
            from secops_ingestion.ssl_util import get_certificate_info
            cert = get_certificate_info(cert_path)
            # Expose only what a health check needs; no container paths, no SANs.
            status["ssl_certificate"] = {
                "subject_cn": cert.get("subject_cn"),
                "valid_until": cert.get("valid_until"),
                "days_remaining": cert.get("days_remaining"),
                "expired": cert.get("days_remaining", 0) <= 0,
            }
        except Exception as exc:
            app.logger.warning("Certificate inspection failed: %s", exc)
    return jsonify(status)


@app.route("/api/settings", methods=["POST"])
@require_secret("ADMIN_SECRET")
def api_settings():
    """Updates project_id or mock_mode. Admin-gated: this mutates runtime state."""
    data = request.get_json(silent=True) or {}

    if "project_id" in data:
        if not isinstance(data["project_id"], str):
            return jsonify({"error": "project_id must be a string"}), 400
        try:
            app.config["SECOPS_PROJECT_ID"] = validate_project_id(data["project_id"])
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400

    if "mock_mode" in data:
        if not isinstance(data["mock_mode"], bool):
            return jsonify({"error": "mock_mode must be a boolean"}), 400
        app.config["MOCK_MODE"] = data["mock_mode"]

    # credentials_path is deliberately no longer settable over HTTP. Accepting a
    # caller-supplied path turned this endpoint into a filesystem probe, and
    # credentials belong in immutable environment configuration.
    if "credentials_path" in data:
        return jsonify({
            "error": "credentials_path is not configurable at runtime",
            "detail": "Set GOOGLE_APPLICATION_CREDENTIALS in the environment instead.",
        }), 400

    _cache.clear()
    _pipeline_cache.clear()
    # Project/mode changed: prior issue history no longer describes this target.
    _issue_trackers.clear()
    return jsonify({
        "success": True,
        "project_id": redact_project_id(app.config["SECOPS_PROJECT_ID"]),
        "mock_mode": app.config["MOCK_MODE"],
    })


@app.route("/api/refresh", methods=["POST"])
@require_secret("ADMIN_SECRET")
def api_refresh():
    """Clears cached metrics and forces reload."""
    _cache.clear()
    _pipeline_cache.clear()
    return jsonify({"success": True, "message": "Cache invalidated."})


@app.route("/api/ingestion/summary")
@rate_limit(_query_limiter)
def api_summary():
    """Returns overall ingestion summary for selected timeframe."""
    period = resolve_period(request.args.get("period", "daily"))
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
@rate_limit(_query_limiter)
def api_breakdown():
    """Returns log sources breakdown list."""
    period = resolve_period(request.args.get("period", "daily"))
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
@rate_limit(_query_limiter)
def api_timeseries():
    """Returns chronological data points for Chart.js visualization."""
    period = resolve_period(request.args.get("period", "daily"))
    summary = fetch_period_summary(period)
    return jsonify({
        "period": summary.period_name,
        "timeline": summary.timeline_series,
    })


@app.route("/api/ingestion/reconcile")
@rate_limit(_query_limiter)
def api_reconcile():
    """Runs customer 30-minute sum vs rollup window reconciliation check."""
    client = get_client()
    period = resolve_period(request.args.get("period", "daily"))
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
@rate_limit(_query_limiter)
def api_bigquery_compat():
    """Returns data formatted according to the legacy BigQuery datalake.ingestion_metrics schema."""
    period = resolve_period(request.args.get("period", "daily"))
    summary = fetch_period_summary(period)
    bq_rows = IngestionCalculator.to_bigquery_compat_table(summary)
    return jsonify({
        "table_name": "chronicle-[REDACTED_PROJECT_ID].datalake.ingestion_metrics",
        "migrated_from": "BigQuery (May 2025)",
        "replacement_api": "Cloud Monitoring API v3 (projects.timeSeries.list)",
        "rows": bq_rows,
    })


def build_pipeline_health(window: str) -> Dict[str, Any]:
    """
    Assemble BindPlane + SecOps issues into one report.

    Each upstream is isolated: a BindPlane or Cloud Monitoring failure becomes a
    critical issue in the report rather than failing the whole response, so one
    outage can never hide the other system's problems.
    """
    now = datetime.now(timezone.utc)
    thresholds = load_thresholds()
    issues = []
    agents_table = []
    delivery_rows = []
    bindplane_info: Dict[str, Any] = {"configured": False}
    bp_data: Optional[Dict[str, Any]] = None

    bp = get_bindplane_client()
    if bp is None:
        bindplane_info["message"] = (
            "BindPlane not configured. Set BINDPLANE_ACCOUNT_ID and BINDPLANE_API_KEY_FILE."
        )
    else:
        bindplane_info = {"configured": True, "mock": isinstance(bp, MockBindPlaneClient)}
        try:
            agents = bp.list_agents()
            configurations = bp.list_configurations()
            agent_ids = [a.get("id", "") for a in agents]
            metrics = bp.agent_throughput(agent_ids, period=window)
            # Delivery always compares a 1h recent window against a 24h baseline.
            metrics_1h = metrics if window == "1h" else bp.agent_throughput(agent_ids, period="1h")
            metrics_24h = bp.agent_throughput(agent_ids, period="24h")
            bp_data = {
                "agents": agents, "configurations": configurations,
                "metrics_1h": metrics_1h, "metrics_24h": metrics_24h,
            }
            bp_issues, agents_table = evaluate_bindplane(agents, configurations, metrics, thresholds)
            issues.extend(bp_issues)
            bindplane_info.update({
                "agent_count": len(agents),
                "connected_count": sum(1 for r in agents_table if r["connected"]),
                "configuration_count": len(configurations),
            })
        except BindPlaneError as exc:
            app.logger.error("BindPlane unavailable: %s", exc)
            issues.append(Issue(
                SEVERITY_CRITICAL, "BINDPLANE_API_UNAVAILABLE", "integration", "bindplane",
                "BindPlane API could not be queried. Collector health is unknown.",
                {"detail": str(exc)},
            ))

    secops_info: Dict[str, Any] = {"available": True}
    secops_bytes_series = None
    secops_log_types: list = []
    secops_end = now
    try:
        raw = fetch_period_raw("daily")
        daily = fetch_period_summary("daily")
        secops_bytes_series = raw.get(METRIC_BYTES_COUNT, [])
        secops_end = datetime.strptime(daily.end_time, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        issues.extend(evaluate_secops_silence(secops_bytes_series, now, thresholds))
        secops_log_types = daily.log_types
        issues.extend(evaluate_secops_parsing(daily.log_types))
    except MonitoringAuthError as exc:
        app.logger.error("Cloud Monitoring unavailable for pipeline health: %s", exc)
        secops_info = {"available": False}
        issues.append(Issue(
            SEVERITY_CRITICAL, "SECOPS_TELEMETRY_UNAVAILABLE", "integration", "secops",
            "SecOps ingestion metrics could not be read from Cloud Monitoring.",
        ))

    delivery_info: Dict[str, Any] = {"available": False}
    collector_ids = bindplane_collector_ids()
    since, until = recent_window(secops_end)
    groups_recent: Dict[Any, Any] = {}
    groups_baseline: Dict[Any, Any] = {}
    if bp_data is not None:
        sources = map_sources(bp_data["configurations"])
        names = {a.get("id", ""): a.get("name") or a.get("hostname") or a.get("id", "")
                 for a in bp_data["agents"]}
        groups_recent = bindplane_bytes_by_group(bp_data["metrics_1h"], sources, 3600, names)
        groups_baseline = bindplane_bytes_by_group(bp_data["metrics_24h"], sources, 86400, names)

    if bp_data is not None and secops_bytes_series is not None:
        delivery_issues, delivery_rows = evaluate_delivery(
            bp_recent=groups_recent,
            bp_baseline=groups_baseline,
            secops_recent=secops_bytes_by_log_type(secops_bytes_series, collector_ids, since, until),
            secops_baseline=secops_bytes_by_log_type(secops_bytes_series, collector_ids, until=until),
            thresholds=thresholds,
        )
        issues.extend(delivery_issues)
        issues.extend(detect_idle_sources(bp_data["agents"], sources, bp_data["metrics_24h"]))
        delivery_info = {
            "available": True,
            "secops_collector_ids": list(collector_ids),
            "mapped_sources": len(sources),
            "recent_window": [since.strftime("%Y-%m-%dT%H:%M:%SZ"), until.strftime("%Y-%m-%dT%H:%M:%SZ")],
        }

    tracker = _issue_trackers.setdefault(window, IssueTracker())
    active, resolved = tracker.update(issues, now)

    unified_rows = build_unified_rows(
        groups_recent=groups_recent,
        groups_baseline=groups_baseline,
        delivery_rows=delivery_rows,
        secops_bytes_series=secops_bytes_series or [],
        secops_log_types=secops_log_types,
        agents_table=agents_table,
        issues=active,
        bindplane_collector_ids=collector_ids,
        recent_since=since,
        recent_until=until,
    )
    return {
        "generated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "window": window,
        "counts": count_by_severity(active),
        "issues": [i.to_dict() for i in active],
        "recently_resolved": [i.to_dict() for i in resolved],
        "agents": agents_table,
        "delivery": delivery_rows,
        "delivery_info": delivery_info,
        "unified": unified_rows,
        "bindplane": bindplane_info,
        "secops": secops_info,
        "thresholds": {
            "drop_warn_pct": thresholds.drop_warn_pct,
            "drop_crit_pct": thresholds.drop_crit_pct,
            "secops_silent_after_minutes": thresholds.secops_silent_after_minutes,
            "delivery_gap_warn_pct": thresholds.delivery_gap_warn_pct,
            "delivery_gap_crit_pct": thresholds.delivery_gap_crit_pct,
        },
    }


@app.route("/api/pipeline/health")
@rate_limit(_query_limiter)
def api_pipeline_health():
    """Collector + ingestion issues for the Pipeline Health panel."""
    window = request.args.get("window", "1h")
    if window not in BINDPLANE_PERIODS:
        window = "1h"

    cache_key = f"{window}_{app.config.get('MOCK_MODE', False)}"
    cached = _pipeline_cache.get(cache_key)
    now = datetime.now(timezone.utc)
    if cached and (now - cached["timestamp"]).total_seconds() < PIPELINE_CACHE_SECONDS:
        return jsonify(cached["report"])

    report = build_pipeline_health(window)
    _pipeline_cache[cache_key] = {"timestamp": now, "report": report}
    return jsonify(report)


# POST-only: as a GET this was triggerable by any crawler or link-preview bot,
# each hit costing four synchronous Cloud Monitoring queries.
@app.route("/api/collect", methods=["POST"])
@require_secret("CRON_SECRET", header_names=("X-Cron-Token",))
@rate_limit(_collect_limiter)
def api_collect():
    """Automated data collection endpoint triggered by Cloud Scheduler or cron."""
    results = {}
    for period_key in ["daily", "weekly", "monthly", "yearly"]:
        summary = fetch_period_summary(period_key, force_reload=True)
        results[period_key] = {
            "period": summary.period_name,
            "total_bytes": summary.total_bytes,
            "total_size_mb": summary.total_size_mb,
            "total_size_gb": summary.total_size_gb,
            "total_records": summary.total_records,
            "total_normalized_events": summary.total_normalized_events,
            "active_sources": summary.active_log_types_count,
            "overall_norm_ratio_pct": summary.overall_norm_ratio,
        }

    return jsonify({
        "status": "success",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "project_id": "[REDACTED_PROJECT_ID]",
        "collected_periods": results,
    })


@app.route("/api/export/csv")
@rate_limit(_query_limiter)
def api_export_csv():
    """Generates and downloads CSV of the active summary."""
    period = resolve_period(request.args.get("period", "daily"))
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
            "log_type": csv_safe(src.log_type),
            "collector_ids": csv_safe(":RQ:".join(src.collectors)),
            "size_mb": src.size_mb,
            "size_gb": src.size_gb,
            "record_count": src.record_count,
            "normalized_event_count": src.normalized_event_count,
            "error_events": src.error_events,
            "normalization_rate_pct": src.normalization_rate_pct,
            "avg_bytes_per_record": src.avg_bytes_per_record,
            "health_status": csv_safe(src.health_status),
        })

    # Both components were previously caller-influenced and flowed unescaped into
    # a response header -- a reflected-file-download vector.
    filename = f"secops_ingestion_{safe_filename_component(period)}.csv"
    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.route("/api/export/json")
@rate_limit(_query_limiter)
def api_export_json():
    """Generates and downloads JSON."""
    period = resolve_period(request.args.get("period", "daily"))
    summary = fetch_period_summary(period)
    filename = f"secops_ingestion_{safe_filename_component(period)}.json"

    data = {
        "project_id": redact_project_id(app.config["SECOPS_PROJECT_ID"]),
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
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
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

    host = os.environ.get("HOST", "0.0.0.0")
    print(f"\n🚀 Launching SecOps Ingestion Dashboard on {protocol}://{host}:{port}")
    app.run(host=host, port=port, debug=False, ssl_context=ssl_context)

