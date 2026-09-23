"""
SecOps Ingestion Calculator
Aggregates timeSeries points, computes Daily/Weekly/Monthly totals,
evaluates ingestion health, and maps to the legacy BigQuery ingestion_metrics schema.
"""
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Any, Optional, Tuple

from .config import (
    METRIC_BYTES_COUNT,
    METRIC_RECORD_COUNT,
    METRIC_NORMALIZER_EVENT_COUNT,
)


def format_bytes(num_bytes: int) -> str:
    """Format bytes into human-readable B, KB, MB, GB, TB string."""
    if num_bytes is None:
        return "0 B"
    for unit in ["B", "KB", "MB", "GB", "TB", "PB"]:
        if abs(num_bytes) < 1000.0:
            return f"{num_bytes:3.2f} {unit}"
        num_bytes /= 1000.0
    return f"{num_bytes:.2f} EB"


def format_number(num: int) -> str:
    """Format large numbers with commas or K/M/B suffixes."""
    if num is None:
        return "0"
    if num >= 1_000_000_000:
        return f"{num / 1_000_000_000:.2f}B"
    if num >= 1_000_000:
        return f"{num / 1_000_000:.2f}M"
    if num >= 1_000:
        return f"{num / 1_000:.2f}K"
    return f"{num:,}"


@dataclass
class LogSourceMetrics:
    """Calculated metrics for a specific log type or collector."""
    log_type: str
    collectors: List[str] = field(default_factory=list)
    bytes_count: int = 0
    record_count: int = 0
    normalized_event_count: int = 0
    size_mb: float = 0.0
    size_gb: float = 0.0
    error_events: int = 0
    normalization_rate_pct: float = 0.0
    avg_bytes_per_record: float = 0.0
    health_status: str = "HEALTHY"
    health_color: str = "green"
    recommendation: str = ""

    def calculate_derived_fields(self):
        """Computes derived metrics, MB/GB sizes, and health status."""
        self.size_mb = round(self.bytes_count / (1000.0 * 1000.0), 3)
        self.size_gb = round(self.bytes_count / (1000.0 * 1000.0 * 1000.0), 4)

        if self.record_count > 0:
            self.avg_bytes_per_record = round(self.bytes_count / self.record_count, 1)
            self.normalization_rate_pct = round((self.normalized_event_count / self.record_count) * 100.0, 2)
            self.error_events = max(0, self.record_count - self.normalized_event_count)
        else:
            self.avg_bytes_per_record = 0.0
            self.normalization_rate_pct = 0.0
            self.error_events = 0

        # Health evaluation
        if self.record_count == 0 and self.bytes_count == 0:
            self.health_status = "INACTIVE"
            self.health_color = "gray"
            self.recommendation = "No telemetry received during this window. Check forwarder or network feed."
        elif self.record_count > 0 and self.normalized_event_count == 0:
            self.health_status = "PARSER_FAILURE"
            self.health_color = "red"
            self.recommendation = "Logs are arriving but 0 events are normalized. Check CBN parser configuration or syntax errors."
        elif self.normalization_rate_pct < 75.0:
            self.health_status = "CRITICAL_DROPS"
            self.health_color = "red"
            self.recommendation = f"Low normalization efficiency ({self.normalization_rate_pct}%). High validation/parsing drop rate."
        elif self.normalization_rate_pct < 95.0:
            self.health_status = "DEGRADED"
            self.health_color = "yellow"
            self.recommendation = f"Partial normalization ({self.normalization_rate_pct}%). Review unparsed raw logs."
        else:
            self.health_status = "HEALTHY"
            self.health_color = "green"
            self.recommendation = "Normal operations. High normalization efficiency."


@dataclass
class AggregationSummary:
    """Summary of all ingestion metrics across an interval."""
    period_name: str
    start_time: str
    end_time: str
    total_bytes: int = 0
    total_records: int = 0
    total_normalized_events: int = 0
    total_size_mb: float = 0.0
    total_size_gb: float = 0.0
    overall_norm_ratio: float = 0.0
    active_log_types_count: int = 0
    healthy_sources_count: int = 0
    warning_sources_count: int = 0
    critical_sources_count: int = 0
    log_types: List[LogSourceMetrics] = field(default_factory=list)
    timeline_series: List[Dict[str, Any]] = field(default_factory=list)


class IngestionCalculator:
    """Processes raw timeSeries points into aggregated reports and BigQuery compat models."""

    @staticmethod
    def _extract_metric_sums(
        raw_series_list: List[Dict[str, Any]],
    ) -> Tuple[Dict[str, int], Dict[str, set], Dict[str, Dict[str, int]]]:
        """
        Extracts total value per log_type and maps collectors.
        Returns:
            (sums_by_log_type, collectors_by_log_type, timeline_buckets)
        """
        sums_by_type: Dict[str, int] = {}
        collectors_by_type: Dict[str, set] = {}
        timeline_buckets: Dict[str, Dict[str, int]] = {}  # interval_end -> {log_type: val}

        for ts in raw_series_list:
            labels = ts.get("resource", {}).get("labels", {})
            log_type = labels.get("log_type", "UNKNOWN")
            collector_id = labels.get("collector_id", "default")

            if log_type not in collectors_by_type:
                collectors_by_type[log_type] = set()
            if collector_id:
                collectors_by_type[log_type].add(collector_id)

            points = ts.get("points", [])
            for pt in points:
                val_dict = pt.get("value", {})
                val = int(val_dict.get("int64Value", 0) or val_dict.get("doubleValue", 0))
                sums_by_type[log_type] = sums_by_type.get(log_type, 0) + val

                end_time = pt.get("interval", {}).get("endTime", "")
                if end_time:
                    if end_time not in timeline_buckets:
                        timeline_buckets[end_time] = {}
                    timeline_buckets[end_time][log_type] = timeline_buckets[end_time].get(log_type, 0) + val

        return sums_by_type, collectors_by_type, timeline_buckets

    @classmethod
    def calculate_summary(
        cls,
        metrics_dict: Dict[str, List[Dict[str, Any]]],
        period_name: str,
        start_time: datetime,
        end_time: datetime,
    ) -> AggregationSummary:
        """
        Combines the 3 metric series into a cohesive Ingestion Summary.
        """
        bytes_series = metrics_dict.get(METRIC_BYTES_COUNT, [])
        record_series = metrics_dict.get(METRIC_RECORD_COUNT, [])
        normalizer_series = metrics_dict.get(METRIC_NORMALIZER_EVENT_COUNT, [])

        bytes_sums, collectors_map, bytes_timeline = cls._extract_metric_sums(bytes_series)
        record_sums, rec_collectors, rec_timeline = cls._extract_metric_sums(record_series)
        norm_sums, _, _ = cls._extract_metric_sums(normalizer_series)

        # Merge collector IDs
        for lt, colls in rec_collectors.items():
            if lt not in collectors_map:
                collectors_map[lt] = set()
            collectors_map[lt].update(colls)

        all_log_types = sorted(
            set(bytes_sums.keys()).union(record_sums.keys()).union(norm_sums.keys()),
            key=lambda lt: bytes_sums.get(lt, 0),
            reverse=True,
        )

        log_source_list = []
        tot_bytes = 0
        tot_records = 0
        tot_normalized = 0
        healthy_cnt = 0
        warning_cnt = 0
        critical_cnt = 0

        for lt in all_log_types:
            b_cnt = bytes_sums.get(lt, 0)
            r_cnt = record_sums.get(lt, 0)
            n_cnt = norm_sums.get(lt, 0)
            colls = sorted(list(collectors_map.get(lt, [])))

            item = LogSourceMetrics(
                log_type=lt,
                collectors=colls,
                bytes_count=b_cnt,
                record_count=r_cnt,
                normalized_event_count=n_cnt,
            )
            item.calculate_derived_fields()
            log_source_list.append(item)

            tot_bytes += b_cnt
            tot_records += r_cnt
            tot_normalized += n_cnt

            if item.health_status == "HEALTHY":
                healthy_cnt += 1
            elif item.health_status in ("DEGRADED", "INACTIVE"):
                warning_cnt += 1
            else:
                critical_cnt += 1

        overall_ratio = round((tot_normalized / tot_records * 100.0), 2) if tot_records > 0 else 0.0

        # Build timeline series for charting (sorted chronologically)
        sorted_times = sorted(bytes_timeline.keys())
        timeline = []
        for t in sorted_times:
            step_bytes = sum(bytes_timeline[t].values())
            step_records = sum(rec_timeline.get(t, {}).values()) if t in rec_timeline else 0
            timeline.append({
                "timestamp": t,
                "bytes": step_bytes,
                "size_mb": round(step_bytes / 1000000.0, 2),
                "records": step_records,
            })

        summary = AggregationSummary(
            period_name=period_name,
            start_time=start_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
            end_time=end_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
            total_bytes=tot_bytes,
            total_records=tot_records,
            total_normalized_events=tot_normalized,
            total_size_mb=round(tot_bytes / (1000.0 * 1000.0), 2),
            total_size_gb=round(tot_bytes / (1000.0 * 1000.0 * 1000.0), 3),
            overall_norm_ratio=overall_ratio,
            active_log_types_count=len([x for x in log_source_list if x.bytes_count > 0]),
            healthy_sources_count=healthy_cnt,
            warning_sources_count=warning_cnt,
            critical_sources_count=critical_cnt,
            log_types=log_source_list,
            timeline_series=timeline,
        )
        return summary

    @classmethod
    def to_bigquery_compat_table(
        cls,
        summary: AggregationSummary,
        delimiter: str = ":RQ:",
    ) -> List[Dict[str, Any]]:
        """
        Emulates the schema of the deprecated BigQuery table:
        chronicle-{customer_code}.datalake.ingestion_metrics

        SELECT log_type,
            string_agg(DISTINCT collector_id, ':RQ:') as collector_ids, 
            SUM(CASE WHEN component = 'Ingestion API' THEN log_volume /1000/1000 END) as size_mb,
            SUM(CASE WHEN component = 'Normalizer' THEN event_count END) as event_count,
            SUM(CASE WHEN component = 'Normalizer' AND state = 'validated' THEN event_count END) as normalized_events,
            SUM(CASE WHEN component = 'Normalizer' AND state = 'failed_validation' ...
        """
        bq_rows = []
        for src in summary.log_types:
            collector_ids_str = delimiter.join(src.collectors) if src.collectors else "default"
            bq_rows.append({
                "log_type": src.log_type,
                "collector_ids": collector_ids_str,
                "input_types": "GCP_MONITORING_API",
                "drop_reason_codes": "NONE" if src.error_events == 0 else "VALIDATION_OR_PARSER_ERROR",
                "size_mb": src.size_mb,
                "size_gb": src.size_gb,
                "event_count": src.record_count,
                "normalized_events": src.normalized_event_count,
                "error_events": src.error_events,
                "parsing_error_events": src.error_events if src.normalized_event_count == 0 else 0,
                "validation_error_events": 0 if src.normalized_event_count == 0 else src.error_events,
                "normalization_rate_pct": src.normalization_rate_pct,
                "health_status": src.health_status,
            })
        return bq_rows
