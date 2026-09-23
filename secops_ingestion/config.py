"""
SecOps Ingestion Metrics Configuration
Project defaults, metric definitions, and time intervals.
"""
from dataclasses import dataclass, field
from typing import List, Dict

# GCP Project and Instance Defaults
DEFAULT_PROJECT_ID = "secops-superweird"
DEFAULT_INSTANCE_ID = "15c91f7d-bce0-4ba4-b253-647e6753f0dd"
DEFAULT_REGION = "us"

# Cloud Monitoring API Base URL
MONITORING_API_BASE = "https://monitoring.googleapis.com/v3"

# Google SecOps Cloud Monitoring Metric Types
METRIC_BYTES_COUNT = "chronicle.googleapis.com/ingestion/log/bytes_count"
METRIC_RECORD_COUNT = "chronicle.googleapis.com/ingestion/log/record_count"
METRIC_NORMALIZER_EVENT_COUNT = "chronicle.googleapis.com/normalizer/event/record_count"

ALL_METRICS = [
    METRIC_BYTES_COUNT,
    METRIC_RECORD_COUNT,
    METRIC_NORMALIZER_EVENT_COUNT,
]

METRIC_LABELS_SHORT = {
    METRIC_BYTES_COUNT: "bytes_count",
    METRIC_RECORD_COUNT: "record_count",
    METRIC_NORMALIZER_EVENT_COUNT: "normalizer_event_count",
}

# Standard Aggregation Settings
DEFAULT_GROUP_BY_FIELDS = [
    "resource.labels.log_type",
    "resource.labels.collector_id",
]

DEFAULT_PER_SERIES_ALIGNER = "ALIGN_SUM"
DEFAULT_CROSS_SERIES_REDUCER = "REDUCE_NONE"

# 30-minute alignment in seconds for customer rollup reconciliation
ROLLUP_30M_SECONDS = 1800

# RFC 3339 / ISO 8601 formatting for Cloud Monitoring API
MONITORING_STRFTIME = "%Y-%m-%dT%H:%M:%SZ"

# Realistic known log types for fallback / simulation matching customer environment
DEFAULT_KNOWN_LOG_TYPES = [
    {"log_type": "PAN_FIREWALL", "collectors": ["observiq-munich", "cribl-munich-9514"], "ratio": 0.991, "avg_bytes": 480},
    {"log_type": "WINEVTLOG", "collectors": ["cribl-edge-windows", "bindplane-hg-windows"], "ratio": 0.985, "avg_bytes": 620},
    {"log_type": "CISCO_MERAKI", "collectors": ["cribl-stream-meraki-10514"], "ratio": 0.962, "avg_bytes": 350},
    {"log_type": "WINDOWS_SYSMON", "collectors": ["bindplane-hg-sysmon", "cribl-edge-sysmon"], "ratio": 0.998, "avg_bytes": 740},
    {"log_type": "LINUX_SYSMON", "collectors": ["bindplane-hg-linux", "syslog-receiver"], "ratio": 0.994, "avg_bytes": 610},
    {"log_type": "AUDITD", "collectors": ["bindplane-hg-linux"], "ratio": 0.978, "avg_bytes": 420},
    {"log_type": "GCP_CLOUDAUDIT", "collectors": ["gcp-pubsub-export"], "ratio": 0.999, "avg_bytes": 1150},
    {"log_type": "GCP_VPC_FLOW", "collectors": ["gcp-pubsub-export"], "ratio": 0.995, "avg_bytes": 280},
    {"log_type": "VMWARE_ESX", "collectors": ["syslog-receiver"], "ratio": 0.945, "avg_bytes": 390},
    {"log_type": "CLOUDFLARE_DNS", "collectors": ["gcs-logpush-feed"], "ratio": 0.990, "avg_bytes": 310},
]
