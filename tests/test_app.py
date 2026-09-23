"""
API and route tests for SecOps Ingestion Flask Application
"""
import json
import unittest

from app import app


class TestAppEndpoints(unittest.TestCase):

    def setUp(self):
        self.app = app.test_client()
        app.config["MOCK_MODE"] = True

    def test_index_route(self):
        res = self.app.get("/")
        self.assertEqual(res.status_code, 200)
        self.assertIn(b"Google SecOps Ingestion Intelligence", res.data)

    def test_api_status(self):
        res = self.app.get("/api/status")
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertIn("project_id", data)
        self.assertEqual(data["mode"], "mock")

    def test_api_summary_daily(self):
        res = self.app.get("/api/ingestion/summary?period=daily")
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertIn("total_bytes", data)
        self.assertIn("total_records", data)
        self.assertIn("total_normalized_events", data)
        self.assertIn("overall_norm_ratio", data)
        self.assertTrue(data["total_bytes"] > 0)

    def test_api_breakdown(self):
        res = self.app.get("/api/ingestion/breakdown?period=daily")
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertIn("log_types", data)
        self.assertTrue(len(data["log_types"]) > 0)

    def test_api_reconcile(self):
        res = self.app.get("/api/ingestion/reconcile?period=daily")
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertIn("reconciliation", data)
        self.assertIn("total_official_max_bytes", data)

    def test_api_bigquery_compat(self):
        res = self.app.get("/api/ingestion/bigquery-compat?period=daily")
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertIn("rows", data)
        self.assertIn("chronicle-secops-superweird.datalake.ingestion_metrics", data["table_name"])

    def test_api_export_csv(self):
        res = self.app.get("/api/export/csv?period=daily")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.mimetype, "text/csv")
        self.assertIn(b"log_type,collector_ids,size_mb", res.data)

    def test_api_export_json(self):
        res = self.app.get("/api/export/json?period=daily")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.mimetype, "application/json")


if __name__ == "__main__":
    unittest.main()
