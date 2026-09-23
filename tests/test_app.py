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

    def test_index_route_ssl(self):
        app.config["SSL_ENABLED"] = True
        res = self.app.get("/")
        self.assertEqual(res.status_code, 200)
        self.assertIn(b"badge-ssl", res.data)
        self.assertIn(b"HTTPS", res.data)
        app.config["SSL_ENABLED"] = False

    def test_api_status(self):
        res = self.app.get("/api/status")
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertIn("project_id", data)
        self.assertEqual(data["mode"], "mock")
        self.assertIn("ssl", data)

    def test_api_status_ssl(self):
        app.config["SSL_ENABLED"] = True
        res = self.app.get("/api/status")
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertTrue(data["ssl"])
        app.config["SSL_ENABLED"] = False

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
        self.assertIn("datalake.ingestion_metrics", data["table_name"])

    def test_api_export_csv(self):
        res = self.app.get("/api/export/csv?period=daily")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.mimetype, "text/csv")
        self.assertIn(b"log_type,collector_ids,size_mb", res.data)

    def test_api_export_json(self):
        res = self.app.get("/api/export/json?period=daily")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.mimetype, "application/json")

    def test_api_summary_yearly(self):
        res = self.app.get("/api/ingestion/summary?period=yearly")
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertIn("total_bytes", data)
        self.assertIn("Yearly (Last 12 Months)", data["period"])
        self.assertTrue(data["total_bytes"] > 0)

    def test_api_summary_12months_alias(self):
        res = self.app.get("/api/ingestion/summary?period=12months")
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertIn("total_bytes", data)
        self.assertIn("Yearly (Last 12 Months)", data["period"])

    def test_api_timeseries_yearly(self):
        res = self.app.get("/api/ingestion/timeseries?period=yearly")
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertIn("timeline", data)
        self.assertTrue(len(data["timeline"]) > 0)

    def test_api_reconcile_yearly(self):
        res = self.app.get("/api/ingestion/reconcile?period=yearly")
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertIn("reconciliation", data)
        self.assertIn("total_official_max_bytes", data)

    def test_api_bigquery_compat_redaction(self):
        res = self.app.get("/api/ingestion/bigquery-compat?period=daily")
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertEqual(data["table_name"], "chronicle-[REDACTED_PROJECT_ID].datalake.ingestion_metrics")

    def test_api_collect_success(self):
        res = self.app.post("/api/collect")
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertEqual(data["status"], "success")
        self.assertIn("collected_periods", data)
        self.assertIn("daily", data["collected_periods"])
        self.assertEqual(data["project_id"], "[REDACTED_PROJECT_ID]")

    def test_api_collect_auth(self):
        import os
        os.environ["CRON_SECRET"] = "supersecret123"
        try:
            # Unauthorized without header
            res_unauth = self.app.post("/api/collect")
            self.assertEqual(res_unauth.status_code, 401)

            # Authorized with X-Cron-Token header
            res_auth = self.app.post("/api/collect", headers={"X-Cron-Token": "supersecret123"})
            self.assertEqual(res_auth.status_code, 200)
            self.assertEqual(res_auth.get_json()["status"], "success")

            # Authorized with Bearer token
            res_bearer = self.app.post("/api/collect", headers={"Authorization": "Bearer supersecret123"})
            self.assertEqual(res_bearer.status_code, 200)
            self.assertEqual(res_bearer.get_json()["status"], "success")
        finally:
            os.environ.pop("CRON_SECRET", None)


if __name__ == "__main__":
    unittest.main()
