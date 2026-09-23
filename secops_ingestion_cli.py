#!/usr/bin/env python3
"""
SecOps Ingestion CLI Tool
Pulls Google SecOps ingestion metrics from Google Cloud Monitoring API.
Calculates Daily, Weekly, and Monthly ingestion data, supports 30m rollup reconciliation,
and exports results to CSV, JSON, Markdown, or launches an interactive web dashboard.
"""
import os
import sys
import argparse
from datetime import datetime, timedelta, timezone

from secops_ingestion.config import (
    DEFAULT_PROJECT_ID,
    ROLLUP_30M_SECONDS,
)
from secops_ingestion.client import SecOpsMonitoringClient
from secops_ingestion.calculator import IngestionCalculator
from secops_ingestion.exporter import IngestionExporter


def parse_args():
    parser = argparse.ArgumentParser(
        description="Pull, calculate, and report Google SecOps ingestion metrics via Cloud Monitoring API."
    )
    parser.add_argument(
        "--project",
        "-p",
        default=os.environ.get("SECOPS_PROJECT_ID", DEFAULT_PROJECT_ID),
        help=f"GCP Project ID (default: {DEFAULT_PROJECT_ID})",
    )
    parser.add_argument(
        "--timeframe",
        "-t",
        choices=["daily", "weekly", "monthly", "yearly", "12months", "all"],
        default="all",
        help="Timeframe to calculate: daily (last 24h), weekly (last 7d), monthly (last 30d), yearly/12months (last 12 months), or all (default)",
    )
    parser.add_argument(
        "--credentials",
        "-c",
        help="Path to GCP Service Account JSON key file (defaults to GOOGLE_APPLICATION_CREDENTIALS)",
    )
    parser.add_argument(
        "--mock",
        "-m",
        action="store_true",
        help="Force mock/synthetic data mode for local testing without live GCP credentials",
    )
    parser.add_argument(
        "--reconcile-30m",
        "-r",
        action="store_true",
        help="Run the customer 30-minute sum vs rollup window reconciliation logic",
    )
    parser.add_argument(
        "--export-csv",
        help="Path to save the summary table as CSV (BigQuery datalake.ingestion_metrics compatible)",
    )
    parser.add_argument(
        "--export-json",
        help="Path to save the complete metrics summary as JSON",
    )
    parser.add_argument(
        "--export-md",
        help="Path to save the report as Markdown",
    )
    parser.add_argument(
        "--serve",
        "-s",
        action="store_true",
        help="Launch the interactive web dashboard site",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=5000,
        help="Port for the web dashboard server (default: 5000)",
    )
    parser.add_argument(
        "--ssl",
        action="store_true",
        help="Enable SSL/HTTPS using a self-signed certificate (auto-generated if missing)",
    )
    parser.add_argument(
        "--ssl-cert",
        default="certs/cert.pem",
        help="Path to SSL certificate file (default: certs/cert.pem)",
    )
    parser.add_argument(
        "--ssl-key",
        default="certs/key.pem",
        help="Path to SSL private key file (default: certs/key.pem)",
    )
    parser.add_argument(
        "--generate-cert",
        action="store_true",
        help="Generate or regenerate a self-signed SSL certificate in certs/ and exit",
    )
    return parser.parse_args()


def calculate_period(client: SecOpsMonitoringClient, period_name: str, days: int):
    now = datetime.now(timezone.utc)
    start_time = now - timedelta(days=days)
    end_time = now

    # Use alignment: 30m for daily, 2h for weekly, 6h for monthly, 1d (86400s) for yearly/12 months
    if days <= 1:
        alignment_seconds = ROLLUP_30M_SECONDS
    elif days <= 7:
        alignment_seconds = 7200
    elif days <= 30:
        alignment_seconds = 21600
    else:
        alignment_seconds = 86400

    print(f"\n[+] Fetching {period_name.upper()} metrics for project '{client.project_id}'...")
    raw_metrics = client.fetch_all_metrics(
        start_time=start_time,
        end_time=end_time,
        alignment_period_seconds=alignment_seconds,
    )
    summary = IngestionCalculator.calculate_summary(
        metrics_dict=raw_metrics,
        period_name=period_name,
        start_time=start_time,
        end_time=end_time,
    )
    return summary


def main():
    args = parse_args()

    # Certificate generation requested
    if args.generate_cert:
        from secops_ingestion.ssl_util import generate_self_signed_cert, get_certificate_info
        cert_p, key_p = generate_self_signed_cert(
            cert_path=args.ssl_cert,
            key_path=args.ssl_key,
            overwrite=True,
        )
        info = get_certificate_info(cert_p)
        print("=" * 80)
        print(" GOOGLE SECOPS INGESTION - SSL CERTIFICATE GENERATOR")
        print("=" * 80)
        print(f" Certificate:    {cert_p}")
        print(f" Private Key:    {key_p}")
        print(f" Common Name:    {info['subject_cn']}")
        print(f" Issuer:         {info['issuer']}")
        print(f" Valid Until:    {info['valid_until']} ({info['days_remaining']} days remaining)")
        print(f" SANs:           {', '.join(info['sans'])}")
        print(f" SHA-256 Finger: {info['fingerprint_sha256'][:32]}...")
        print("=" * 80)
        print("✅ Self-signed SSL certificate is ready for HTTPS.\n")
        if not args.serve:
            return

    # If --serve is requested, launch the web application
    if args.serve:
        try:
            from app import app
        except ImportError as exc:
            print(
                f"\n[ERROR] Flask or its dependencies are not installed ({exc}).\n"
                "To run the web dashboard, install project dependencies:\n"
                "    pip install -r requirements.txt\n",
                file=sys.stderr,
            )
            sys.exit(1)

        ssl_context = None
        protocol = "http"
        if args.ssl:
            from secops_ingestion.ssl_util import ensure_ssl_credentials, create_ssl_context
            cert_p, key_p = ensure_ssl_credentials(
                cert_path=args.ssl_cert,
                key_path=args.ssl_key,
            )
            ssl_context = create_ssl_context(cert_p, key_p)
            protocol = "https"
            app.config["SSL_ENABLED"] = True
            app.config["SSL_CERT_PATH"] = cert_p
            app.config["SSL_KEY_PATH"] = key_p

        print(f"\n🚀 Launching SecOps Ingestion Dashboard on {protocol}://localhost:{args.port} (Project: {args.project})")
        if args.ssl:
            print(f"🔒 SSL/TLS Enabled (Cert: {cert_p}, Key: {key_p})")
        app.config["SECOPS_PROJECT_ID"] = args.project
        app.config["MOCK_MODE"] = args.mock
        app.run(host="0.0.0.0", port=args.port, debug=False, ssl_context=ssl_context)
        return

    client = SecOpsMonitoringClient(
        project_id=args.project,
        credentials_path=args.credentials,
        mock_mode=args.mock,
    )

    auth_status = client.get_auth_status()
    print("=" * 80)
    print(" GOOGLE SECOPS INGESTION METRICS COLLECTOR")
    print(f" Target Project: {client.project_id}")
    print(f" Auth Mode:      {auth_status['mode'].upper()} - {auth_status['message']}")
    print("=" * 80)

    summaries = []

    periods_to_run = []
    if args.timeframe in ("daily", "all"):
        periods_to_run.append(("Daily (Last 24 Hours)", 1))
    if args.timeframe in ("weekly", "all"):
        periods_to_run.append(("Weekly (Last 7 Days)", 7))
    if args.timeframe in ("monthly", "all"):
        periods_to_run.append(("Monthly (Last 30 Days)", 30))
    if args.timeframe in ("yearly", "12months", "all"):
        periods_to_run.append(("Yearly (Last 12 Months)", 365))

    for name, days in periods_to_run:
        summary = calculate_period(client, name, days)
        summaries.append(summary)
        IngestionExporter.print_cli_summary(summary)

    # Optional 30-minute sum vs rollup reconciliation
    if args.reconcile_30m:
        print("\n[+] Running Customer Rollup Reconciliation Check (30m Sum vs Full Rollup)...")
        reconcile_data = client.reconcile_30m_vs_rollup()
        IngestionExporter.print_reconciliation_cli(reconcile_data)

    # Handle Exports (exports the primary requested summary, or the last one)
    if summaries:
        target_summary = summaries[0]
        if args.export_csv:
            IngestionExporter.export_csv(target_summary, args.export_csv)
            print(f"[✓] Successfully exported CSV to: {args.export_csv}")

        if args.export_json:
            IngestionExporter.export_json(target_summary, args.export_json)
            print(f"[✓] Successfully exported JSON to: {args.export_json}")

        if args.export_md:
            md_content = IngestionExporter.to_markdown(target_summary)
            with open(args.export_md, "w", encoding="utf-8") as f:
                f.write(md_content)
            print(f"[✓] Successfully exported Markdown to: {args.export_md}")


if __name__ == "__main__":
    main()
