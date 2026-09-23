"""
Unit tests for secops_ingestion_cli
"""
import sys
import unittest
from unittest.mock import patch, MagicMock

import secops_ingestion_cli


class TestSecopsIngestionCLI(unittest.TestCase):

    def test_parse_args_defaults(self):
        with patch.object(sys, "argv", ["secops_ingestion_cli.py"]):
            args = secops_ingestion_cli.parse_args()
            self.assertEqual(args.project, "your-secops-project-id")
            self.assertEqual(args.timeframe, "all")
            self.assertEqual(args.port, 5000)
            self.assertFalse(args.serve)
            self.assertFalse(args.mock)
            self.assertFalse(args.reconcile_30m)

    def test_parse_args_custom(self):
        test_args = [
            "secops_ingestion_cli.py",
            "--project", "test-project-123",
            "--timeframe", "daily",
            "--serve",
            "--port", "8080",
            "--mock",
            "--reconcile-30m",
            "--export-csv", "output.csv",
        ]
        with patch.object(sys, "argv", test_args):
            args = secops_ingestion_cli.parse_args()
            self.assertEqual(args.project, "test-project-123")
            self.assertEqual(args.timeframe, "daily")
            self.assertEqual(args.port, 8080)
            self.assertTrue(args.serve)
            self.assertTrue(args.mock)
            self.assertTrue(args.reconcile_30m)
            self.assertEqual(args.export_csv, "output.csv")

    @patch("sys.stderr")
    @patch("secops_ingestion_cli.parse_args")
    def test_serve_missing_flask_handles_gracefully(self, mock_parse_args, mock_stderr):
        mock_args = MagicMock()
        mock_args.serve = True
        mock_args.ssl = False
        mock_args.generate_cert = False
        mock_args.port = 5000
        mock_args.project = "test-project"
        mock_args.mock = True
        mock_args.ssl_cert = "certs/cert.pem"
        mock_args.ssl_key = "certs/key.pem"
        mock_parse_args.return_value = mock_args

        with patch.dict(sys.modules, {"app": None}):
            with self.assertRaises(SystemExit) as ctx:
                secops_ingestion_cli.main()
            self.assertEqual(ctx.exception.code, 1)

    @patch("builtins.print")
    @patch("secops_ingestion_cli.parse_args")
    def test_serve_launches_app(self, mock_parse_args, mock_print):
        mock_args = MagicMock()
        mock_args.serve = True
        mock_args.ssl = False
        mock_args.generate_cert = False
        mock_args.port = 8080
        mock_args.project = "test-project"
        mock_args.mock = True
        mock_args.ssl_cert = "certs/cert.pem"
        mock_args.ssl_key = "certs/key.pem"
        mock_parse_args.return_value = mock_args

        mock_app = MagicMock()
        mock_app.config = {}
        with patch.dict(sys.modules, {"app": MagicMock(app=mock_app)}):
            secops_ingestion_cli.main()
            self.assertEqual(mock_app.config.get("SECOPS_PROJECT_ID"), "test-project")
            self.assertTrue(mock_app.config.get("MOCK_MODE"))
            mock_app.run.assert_called_once_with(host="0.0.0.0", port=8080, debug=False, ssl_context=None)

    def test_parse_args_ssl(self):
        test_args = [
            "secops_ingestion_cli.py",
            "--serve",
            "--ssl",
            "--ssl-cert", "custom/cert.pem",
            "--ssl-key", "custom/key.pem",
            "--generate-cert",
        ]
        with patch.object(sys, "argv", test_args):
            args = secops_ingestion_cli.parse_args()
            self.assertTrue(args.ssl)
            self.assertTrue(args.generate_cert)
            self.assertEqual(args.ssl_cert, "custom/cert.pem")
            self.assertEqual(args.ssl_key, "custom/key.pem")

    @patch("builtins.print")
    @patch("secops_ingestion.ssl_util.get_certificate_info")
    @patch("secops_ingestion.ssl_util.generate_self_signed_cert")
    @patch("secops_ingestion_cli.parse_args")
    def test_generate_cert_flag(self, mock_parse_args, mock_gen_cert, mock_cert_info, mock_print):
        mock_args = MagicMock()
        mock_args.generate_cert = True
        mock_args.serve = False
        mock_args.ssl_cert = "certs/cert.pem"
        mock_args.ssl_key = "certs/key.pem"
        mock_parse_args.return_value = mock_args
        mock_gen_cert.return_value = ("certs/cert.pem", "certs/key.pem")
        mock_cert_info.return_value = {
            "subject_cn": "localhost",
            "issuer": "CN=localhost",
            "valid_until": "2027-09-23T00:00:00Z",
            "days_remaining": 365,
            "sans": ["localhost", "127.0.0.1"],
            "fingerprint_sha256": "abcdef123456",
        }

        secops_ingestion_cli.main()
        mock_gen_cert.assert_called_once_with(
            cert_path="certs/cert.pem",
            key_path="certs/key.pem",
            overwrite=True,
        )

    @patch("builtins.print")
    @patch("secops_ingestion.ssl_util.create_ssl_context")
    @patch("secops_ingestion.ssl_util.ensure_ssl_credentials")
    @patch("secops_ingestion_cli.parse_args")
    def test_serve_with_ssl(self, mock_parse_args, mock_ensure_ssl, mock_create_ctx, mock_print):
        mock_args = MagicMock()
        mock_args.generate_cert = False
        mock_args.serve = True
        mock_args.ssl = True
        mock_args.port = 8443
        mock_args.project = "test-project"
        mock_args.mock = True
        mock_args.ssl_cert = "certs/cert.pem"
        mock_args.ssl_key = "certs/key.pem"
        mock_parse_args.return_value = mock_args

        mock_ensure_ssl.return_value = ("certs/cert.pem", "certs/key.pem")
        fake_ssl_ctx = MagicMock()
        mock_create_ctx.return_value = fake_ssl_ctx

        mock_app = MagicMock()
        mock_app.config = {}
        with patch.dict(sys.modules, {"app": MagicMock(app=mock_app)}):
            secops_ingestion_cli.main()
            self.assertTrue(mock_app.config.get("SSL_ENABLED"))
            mock_app.run.assert_called_once_with(
                host="0.0.0.0",
                port=8443,
                debug=False,
                ssl_context=fake_ssl_ctx,
            )

    def test_parse_args_timeframe_yearly(self):
        with patch.object(sys, "argv", ["secops_ingestion_cli.py", "--timeframe", "yearly"]):
            args = secops_ingestion_cli.parse_args()
            self.assertEqual(args.timeframe, "yearly")

        with patch.object(sys, "argv", ["secops_ingestion_cli.py", "--timeframe", "12months"]):
            args = secops_ingestion_cli.parse_args()
            self.assertEqual(args.timeframe, "12months")

    @patch("builtins.print")
    @patch("secops_ingestion_cli.IngestionCalculator.calculate_summary")
    def test_calculate_period_yearly_alignment(self, mock_calc, mock_print):
        mock_client = MagicMock()
        mock_client.project_id = "test-project"
        mock_client.fetch_all_metrics.return_value = {}

        secops_ingestion_cli.calculate_period(mock_client, "Yearly (Last 12 Months)", 365)
        mock_client.fetch_all_metrics.assert_called_once()
        _, kwargs = mock_client.fetch_all_metrics.call_args
        self.assertEqual(kwargs.get("alignment_period_seconds"), 86400)


if __name__ == "__main__":
    unittest.main()
