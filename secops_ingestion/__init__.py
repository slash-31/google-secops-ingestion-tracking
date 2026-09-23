"""
SecOps Ingestion Package
Provides tools to pull, reconcile, calculate, and visualize Google SecOps ingestion metrics.
"""
from .client import SecOpsMonitoringClient, MonitoringAuthError
from .calculator import IngestionCalculator, AggregationSummary, LogSourceMetrics, format_bytes, format_number
from .exporter import IngestionExporter
from .ssl_util import (
    generate_self_signed_cert,
    ensure_ssl_credentials,
    create_ssl_context,
    get_certificate_info,
)
from .config import (
    DEFAULT_PROJECT_ID,
    METRIC_BYTES_COUNT,
    METRIC_RECORD_COUNT,
    METRIC_NORMALIZER_EVENT_COUNT,
    PERIOD_CONFIGS,
)

__all__ = [
    "SecOpsMonitoringClient",
    "MonitoringAuthError",
    "IngestionCalculator",
    "AggregationSummary",
    "LogSourceMetrics",
    "IngestionExporter",
    "generate_self_signed_cert",
    "ensure_ssl_credentials",
    "create_ssl_context",
    "get_certificate_info",
    "DEFAULT_PROJECT_ID",
    "METRIC_BYTES_COUNT",
    "METRIC_RECORD_COUNT",
    "METRIC_NORMALIZER_EVENT_COUNT",
    "PERIOD_CONFIGS",
    "format_bytes",
    "format_number",
]
