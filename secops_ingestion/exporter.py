"""
SecOps Ingestion Exporter
Generates ASCII CLI tables, CSV exports, JSON payloads, and Markdown reports.
"""
import csv
import json
from typing import List, Dict, Any, Optional

from .calculator import AggregationSummary, format_bytes, format_number


class IngestionExporter:
    """Exports ingestion summaries and reconciliation reports to various formats."""

    @staticmethod
    def print_cli_summary(summary: AggregationSummary):
        """Prints a rich, formatted terminal dashboard view."""
        border = "=" * 90
        sub_border = "-" * 90

        print("\n" + border)
        print(f" GOOGLE SECOPS INGESTION REPORT: {summary.period_name.upper()}")
        print(f" Time Range: {summary.start_time} --> {summary.end_time}")
        print(border)

        print("\n--- [ OVERALL INGESTION METRICS ] ---")
        print(f"  * Total Volume:            {format_bytes(summary.total_bytes)} ({summary.total_size_gb:,.2f} GB / {summary.total_size_mb:,.1f} MB)")
        print(f"  * Total Records (Raw):     {format_number(summary.total_records)} records")
        print(f"  * Total Normalized Events: {format_number(summary.total_normalized_events)} UDM events")
        print(f"  * Normalization Success:   {summary.overall_norm_ratio:.2f}%")
        print(f"  * Active Log Types:        {summary.active_log_types_count}")
        print(f"  * Health Breakdown:        {summary.healthy_sources_count} Healthy, {summary.warning_sources_count} Degraded, {summary.critical_sources_count} Critical")

        print("\n--- [ LOG TYPE BREAKDOWN (Sorted by Volume) ] ---")
        header = f"{'LOG TYPE':<20} | {'VOLUME (MB)':>12} | {'RECORDS':>10} | {'NORMALIZED':>10} | {'NORM %':>7} | {'HEALTH':<12}"
        print(header)
        print(sub_border)

        for src in summary.log_types:
            norm_str = f"{src.normalization_rate_pct:.1f}%"
            print(f"{src.log_type:<20} | {src.size_mb:>12,f} | {format_number(src.record_count):>10} | {format_number(src.normalized_event_count):>10} | {norm_str:>7} | {src.health_status:<12}")

        print(border + "\n")

    @staticmethod
    def print_reconciliation_cli(reconcile_data: List[Dict[str, Any]]):
        """Prints the customer's 30-minute sum vs rollup window comparison."""
        border = "=" * 95
        sub_border = "-" * 95

        print("\n" + border)
        print(" SECOPS ROLLUP RECONCILIATION AUDIT (30m Sum vs Full Rollup Window)")
        print(border)

        header = f"{'LOG TYPE':<18} | {'COLLECTOR ID':<22} | {'30M SUM':>12} | {'ROLLUP':>12} | {'OFFICIAL MAX':>13} | {'WINNER':<10}"
        print(header)
        print(sub_border)

        for row in reconcile_data:
            print(f"{row['log_type']:<18} | {row['collector_id']:<22} | {format_bytes(row['sum_30m']):>12} | {format_bytes(row['rollup']):>12} | {format_bytes(row['official_max']):>13} | {row['winner']:<10}")

        print(border + "\n")

    @staticmethod
    def export_csv(summary: AggregationSummary, output_path: str):
        """Writes BigQuery-compatible summary to CSV."""
        fieldnames = [
            "log_type",
            "collector_ids",
            "size_mb",
            "size_gb",
            "bytes_count",
            "record_count",
            "normalized_event_count",
            "error_events",
            "normalization_rate_pct",
            "avg_bytes_per_record",
            "health_status",
            "recommendation",
        ]
        with open(output_path, mode="w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for src in summary.log_types:
                writer.writerow({
                    "log_type": src.log_type,
                    "collector_ids": ":RQ:".join(src.collectors),
                    "size_mb": src.size_mb,
                    "size_gb": src.size_gb,
                    "bytes_count": src.bytes_count,
                    "record_count": src.record_count,
                    "normalized_event_count": src.normalized_event_count,
                    "error_events": src.error_events,
                    "normalization_rate_pct": src.normalization_rate_pct,
                    "avg_bytes_per_record": src.avg_bytes_per_record,
                    "health_status": src.health_status,
                    "recommendation": src.recommendation,
                })

    @staticmethod
    def export_json(summary: AggregationSummary, output_path: str):
        """Exports summary data to JSON."""
        data = {
            "period": summary.period_name,
            "interval": {
                "start_time": summary.start_time,
                "end_time": summary.end_time,
            },
            "totals": {
                "bytes": summary.total_bytes,
                "size_mb": summary.total_size_mb,
                "size_gb": summary.total_size_gb,
                "records": summary.total_records,
                "normalized_events": summary.total_normalized_events,
                "overall_normalization_rate_pct": summary.overall_norm_ratio,
            },
            "health_summary": {
                "active_log_types": summary.active_log_types_count,
                "healthy": summary.healthy_sources_count,
                "warning": summary.warning_sources_count,
                "critical": summary.critical_sources_count,
            },
            "log_types": [
                {
                    "log_type": src.log_type,
                    "collectors": src.collectors,
                    "bytes": src.bytes_count,
                    "size_mb": src.size_mb,
                    "size_gb": src.size_gb,
                    "records": src.record_count,
                    "normalized_events": src.normalized_event_count,
                    "error_events": src.error_events,
                    "normalization_rate_pct": src.normalization_rate_pct,
                    "avg_bytes_per_record": src.avg_bytes_per_record,
                    "health_status": src.health_status,
                    "recommendation": src.recommendation,
                }
                for src in summary.log_types
            ],
            "timeline": summary.timeline_series,
        }
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

    @staticmethod
    def to_markdown(summary: AggregationSummary) -> str:
        """Renders GitHub Flavored Markdown report."""
        lines = [
            f"# Google SecOps Ingestion Summary ({summary.period_name.upper()})",
            f"**Interval:** `{summary.start_time}` to `{summary.end_time}`\n",
            "## Key Metrics",
            f"- **Total Volume:** `{format_bytes(summary.total_bytes)}` ({summary.total_size_gb:,.2f} GB)",
            f"- **Raw Ingested Records:** `{format_number(summary.total_records)}`",
            f"- **Normalized UDM Events:** `{format_number(summary.total_normalized_events)}`",
            f"- **Overall Normalization Efficiency:** `{summary.overall_norm_ratio:.2f}%`",
            f"- **Active Ingestion Sources:** `{summary.active_log_types_count}`\n",
            "## Log Sources Breakdown",
            "| Log Type | Volume (MB) | Records | Normalized Events | Norm % | Health Status |",
            "| :--- | :---: | :---: | :---: | :---: | :--- |",
        ]
        for src in summary.log_types:
            lines.append(
                f"| `{src.log_type}` | {src.size_mb:,.2f} | {src.record_count:,} | {src.normalized_event_count:,} | {src.normalization_rate_pct:.1f}% | `{src.health_status}` |"
            )
        return "\n".join(lines)
