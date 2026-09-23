"""
Unit tests for SecOps Ingestion Calculator
"""
import unittest
from datetime import datetime, timedelta, timezone

from secops_ingestion.config import (
    METRIC_BYTES_COUNT,
    METRIC_RECORD_COUNT,
    METRIC_NORMALIZER_EVENT_COUNT,
)
from secops_ingestion.calculator import (
    IngestionCalculator,
    format_bytes,
    format_number,
    LogSourceMetrics,
)
from secops_ingestion.client import SecOpsMonitoringClient


class TestIngestionCalculator(unittest.TestCase):

    def test_format_bytes(self):
        self.assertEqual(format_bytes(500), "500.00 B")
        self.assertEqual(format_bytes(1000 * 1000), "1.00 MB")
        self.assertEqual(format_bytes(1000 * 1000 * 1000), "1.00 GB")
        self.assertEqual(format_bytes(1000 * 1000 * 1000 * 1000), "1.00 TB")

    def test_format_number(self):
        self.assertEqual(format_number(450), "450")
        self.assertEqual(format_number(1250), "1.25K")
        self.assertEqual(format_number(3500000), "3.50M")
        self.assertEqual(format_number(2100000000), "2.10B")

    def test_derived_fields(self):
        src = LogSourceMetrics(
            log_type="PAN_FIREWALL",
            collectors=["observiq-1"],
            bytes_count=100_000_000,
            record_count=200_000,
            normalized_event_count=196_000,
        )
        src.calculate_derived_fields()

        self.assertEqual(src.size_mb, 100.0)
        self.assertEqual(src.size_gb, 0.1)
        self.assertEqual(src.normalization_rate_pct, 98.0)
        self.assertEqual(src.error_events, 4_000)
        self.assertEqual(src.avg_bytes_per_record, 500.0)
        self.assertEqual(src.health_status, "HEALTHY")

    def test_reconciliation_logic(self):
        client = SecOpsMonitoringClient(project_id="secops-superweird", mock_mode=True)
        now = datetime.now(timezone.utc)
        reconcile_data = client.reconcile_30m_vs_rollup(
            start_time=now - timedelta(days=1),
            end_time=now,
        )
        self.assertTrue(len(reconcile_data) > 0)
        for row in reconcile_data:
            self.assertIn("log_type", row)
            self.assertIn("collector_id", row)
            self.assertIn("official_max", row)
            self.assertEqual(row["official_max"], max(row["sum_30m"], row["rollup"]))

    def test_bigquery_compat_table(self):
        client = SecOpsMonitoringClient(project_id="secops-superweird", mock_mode=True)
        now = datetime.now(timezone.utc)
        raw_metrics = client.fetch_all_metrics(
            start_time=now - timedelta(days=1),
            end_time=now,
        )
        summary = IngestionCalculator.calculate_summary(
            metrics_dict=raw_metrics,
            period_name="Daily",
            start_time=now - timedelta(days=1),
            end_time=now,
        )
        bq_rows = IngestionCalculator.to_bigquery_compat_table(summary)

        self.assertTrue(len(bq_rows) > 0)
        for row in bq_rows:
            self.assertIn("log_type", row)
            self.assertIn("collector_ids", row)
            self.assertIn("size_mb", row)
            self.assertIn("event_count", row)
            self.assertIn("normalized_events", row)


if __name__ == "__main__":
    unittest.main()
