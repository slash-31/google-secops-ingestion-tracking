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
        import os
        os.environ["CRON_SECRET"] = "supersecret123"
        try:
            res = self.app.post("/api/collect", headers={"X-Cron-Token": "supersecret123"})
            self.assertEqual(res.status_code, 200)
            data = res.get_json()
            self.assertEqual(data["status"], "success")
            self.assertIn("collected_periods", data)
            self.assertIn("daily", data["collected_periods"])
            self.assertEqual(data["project_id"], "[REDACTED_PROJECT_ID]")
        finally:
            os.environ.pop("CRON_SECRET", None)

    def test_api_collect_denied_when_secret_unset(self):
        """Deny-by-default: an unconfigured CRON_SECRET disables the endpoint."""
        import os
        os.environ.pop("CRON_SECRET", None)
        res = self.app.post("/api/collect")
        self.assertEqual(res.status_code, 503)

    def test_api_collect_rejects_get(self):
        """GET made this crawler-triggerable; only POST is accepted now."""
        import os
        os.environ["CRON_SECRET"] = "supersecret123"
        try:
            res = self.app.get("/api/collect", headers={"X-Cron-Token": "supersecret123"})
            self.assertEqual(res.status_code, 405)
        finally:
            os.environ.pop("CRON_SECRET", None)

    def test_api_settings_requires_admin_secret(self):
        import os
        os.environ.pop("ADMIN_SECRET", None)
        res = self.app.post("/api/settings", json={"mock_mode": True})
        self.assertEqual(res.status_code, 503)

        os.environ["ADMIN_SECRET"] = "admintoken"
        try:
            unauth = self.app.post("/api/settings", json={"mock_mode": True})
            self.assertEqual(unauth.status_code, 401)

            ok = self.app.post(
                "/api/settings",
                json={"mock_mode": True},
                headers={"Authorization": "Bearer admintoken"},
            )
            self.assertEqual(ok.status_code, 200)
        finally:
            os.environ.pop("ADMIN_SECRET", None)
            app.config["MOCK_MODE"] = True

    def test_api_settings_rejects_credentials_path(self):
        """credentials_path was a filesystem probe; it is no longer settable."""
        import os
        os.environ["ADMIN_SECRET"] = "admintoken"
        try:
            res = self.app.post(
                "/api/settings",
                json={"credentials_path": "/etc/hosts"},
                headers={"Authorization": "Bearer admintoken"},
            )
            self.assertEqual(res.status_code, 400)
        finally:
            os.environ.pop("ADMIN_SECRET", None)

    def test_api_settings_rejects_invalid_project_id(self):
        import os
        os.environ["ADMIN_SECRET"] = "admintoken"
        try:
            for bad in ["../../etc", "UPPER-CASE", "x", "proj id"]:
                res = self.app.post(
                    "/api/settings",
                    json={"project_id": bad},
                    headers={"Authorization": "Bearer admintoken"},
                )
                self.assertEqual(res.status_code, 400, f"accepted bad id: {bad}")
        finally:
            os.environ.pop("ADMIN_SECRET", None)

    def test_api_refresh_requires_admin_secret(self):
        import os
        os.environ.pop("ADMIN_SECRET", None)
        res = self.app.post("/api/refresh")
        self.assertEqual(res.status_code, 503)

    def test_status_does_not_leak_token_or_project(self):
        res = self.app.get("/api/status")
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertNotIn("token_preview", data)
        self.assertNotIn("error", data)
        self.assertIn("*", data["project_id"])
        cert = data.get("ssl_certificate")
        if cert is not None:
            self.assertNotIn("cert_path", cert)
            self.assertNotIn("sans", cert)

    def test_security_headers_present(self):
        res = self.app.get("/")
        for header in [
            "X-Content-Type-Options",
            "X-Frame-Options",
            "Content-Security-Policy",
            "Strict-Transport-Security",
            "Referrer-Policy",
            "Permissions-Policy",
        ]:
            self.assertIn(header, res.headers, f"missing {header}")
        self.assertIn("frame-ancestors 'none'", res.headers["Content-Security-Policy"])

    def test_export_filename_is_not_header_injectable(self):
        res = self.app.get('/api/export/csv?period=daily";filename="evil.html')
        disposition = res.headers.get("Content-Disposition", "")
        # Unknown period collapses to the canonical key, so nothing caller-supplied
        # reaches the header at all.
        self.assertEqual(disposition, 'attachment; filename="secops_ingestion_daily.csv"')
        self.assertEqual(disposition.count("filename="), 1)

    def test_csv_formula_injection_is_neutralised(self):
        from secops_ingestion.security import csv_safe
        self.assertEqual(csv_safe("=1+1"), "'=1+1")
        self.assertEqual(csv_safe("@SUM(A1)"), "'@SUM(A1)")
        self.assertEqual(csv_safe("PAN_FIREWALL"), "PAN_FIREWALL")

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
