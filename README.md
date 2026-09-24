# Google SecOps Ingestion Intelligence (Script & Dashboard)

[![Python 3.9+](https://img.shields.io/badge/python-3.9+-blue.svg)](https://www.python.org/downloads/)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](https://opensource.org/licenses/Apache-2.0)
[![Docker](https://img.shields.io/badge/docker-ready-blue.svg)](https://www.docker.com/)

A production-ready CLI script and interactive web dashboard to pull, calculate, reconcile, and visualize daily, weekly, monthly, and yearly ingestion telemetry from **Google SecOps (formerly Chronicle)** via the **Google Cloud Monitoring API** (`v3 projects.timeSeries.list`).

---

## 📑 Table of Contents

- [Overview & Why This Exists](#-overview--why-this-exists)
- [Key Capabilities](#-key-capabilities)
- [Architecture & Metric Extraction](#-architecture--metric-extraction)
- [Prerequisites & Dependencies (Zero Assumptions Guide)](#-prerequisites--dependencies-zero-assumptions-guide)
- [IAM & Service Account Setup (Least Privilege)](#-iam--service-account-setup-least-privilege)
  - [Step 1: Enable Cloud Monitoring API](#step-1-enable-cloud-monitoring-api)
  - [Step 2: Create a Dedicated Service Account](#step-2-create-a-dedicated-service-account)
  - [Step 3: Grant Least-Privilege IAM Roles](#step-3-grant-least-privilege-iam-roles)
  - [Step 4: Provision Credentials](#step-4-provision-credentials)
- [Environment Variables & Configuration](#-environment-variables--configuration)
- [Installation & Quick Start](#-installation--quick-start)
  - [Method 1: Local Python (Native)](#method-1-local-python-native)
  - [Method 2: Docker Container](#method-2-docker-container)
  - [Method 3: Docker Compose](#method-3-docker-compose)
  - [Method 4: CLI Reporting & Exports](#method-4-cli-reporting--exports)
- [Automated Telemetry Collection & Scheduling](#-automated-telemetry-collection--scheduling)
- [REST API Reference](#-rest-api-reference)
- [Testing & Quality Assurance](#-testing--quality-assurance)
- [Security & Redaction](#-security--redaction)
- [Release Notes & Changelog](#-release-notes--changelog)

---

## 💡 Overview & Why This Exists

In May 2025, Google migrated historical SecOps ingestion telemetry away from legacy BigQuery exports (`datalake.ingestion_metrics`) to native **Google Cloud Monitoring API** time-series streams. 

Security teams and detection engineers need visibility into:
1. **Raw Volume**: How many gigabytes/terabytes of logs are received each day/month.
2. **Parser Health**: How many raw records successfully normalize into Google SecOps Unified Data Model (UDM) events vs. fail due to parser syntax errors.
3. **Rollup Reconciliation**: Reconciling fine-grained 30-minute metric buckets against whole-window rollup summaries to eliminate ingestion measurement discrepancies.

This project delivers a **complete, standalone monitoring suite**—including a CLI reporting tool, automated collector, and dark-themed web dashboard—with **zero mandatory infrastructure lock-in**.

---

## 🌟 Key Capabilities

- **Native Cloud Monitoring v3 Extraction**: Directly queries the `timeSeries.list` endpoint for:
  - `chronicle.googleapis.com/ingestion/log/bytes_count` (Raw Ingestion Volume - Bytes, MB, GB, TB)
  - `chronicle.googleapis.com/ingestion/log/record_count` (Raw Ingested Records / Log Lines)
  - `chronicle.googleapis.com/normalizer/event/record_count` (Normalized UDM Events Produced)
- **Flexible Timeframe Aggregations**:
  - **Daily**: Last 24 hours (30-minute alignment)
  - **Weekly**: Last 7 days (2-hour alignment)
  - **Monthly**: Last 30 days (6-hour alignment)
  - **Yearly / 12 Months**: Last 365 days (24-hour alignment)
- **Authoritative 30m Reconciliation**: Computes `max(sum_30m, rollup)` per log source to guarantee 100% accurate billing and ingestion tracking.
- **Legacy BigQuery Compatibility**: Recreates the schema of the retired `datalake.ingestion_metrics` table on the fly for backwards compatibility with existing pipelines.
- **Interactive Dark-Themed Web Dashboard**: Real-time KPI summary cards, volume share donut charts, time-series graphs, and searchable log-type tables.
- **Offline / Mock Mode Engine**: Includes high-fidelity synthetic demo telemetry (simulating Palo Alto, Windows Sysmon, Linux Auditd, Meraki, GCP VPC Flow, AWS CloudTrail) allowing immediate testing without an active GCP project.

---

## 📐 Architecture & Metric Extraction

```
┌─────────────────────────────────────────────────────────────┐
│                      Google SecOps                          │
│         (Forwarders, Ingestion API, Cloud Feeds)            │
└──────────────────────────────┬──────────────────────────────┘
                               │ Streams telemetry
                               ▼
┌─────────────────────────────────────────────────────────────┐
│            Google Cloud Monitoring (Metrics API v3)         │
│  - chronicle.googleapis.com/ingestion/log/bytes_count       │
│  - chronicle.googleapis.com/ingestion/log/record_count      │
│  - chronicle.googleapis.com/normalizer/event/record_count   │
└──────────────────────────────┬──────────────────────────────┘
                               │ Authenticated API Query (OAuth2 / JWT)
                               ▼
┌─────────────────────────────────────────────────────────────┐
│        SecOps Ingestion Intelligence Suite (This App)       │
│                                                             │
│   ┌─────────────────────┐       ┌───────────────────────┐   │
│   │ CLI Tool (CLI/Cron) │       │ Flask REST App (UI)   │   │
│   └──────────┬──────────┘       └───────────┬───────────┘   │
│              │                              │               │
│              ▼                              ▼               │
│   ┌─────────────────────────────────────────────────────┐   │
│   │            Reconciliation Engine                    │   │
│   │   val = max(sum_30m_buckets, window_rollup)         │   │
│   └─────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────┘
```

The application queries the Cloud Monitoring API using the following pattern:
```http
GET https://monitoring.googleapis.com/v3/projects/{SECOPS_PROJECT_ID}/timeSeries?
  filter=metric.type = "chronicle.googleapis.com/{metric_type}"
  &aggregation.groupByFields=resource.labels.log_type, resource.labels.collector_id
  &aggregation.crossSeriesReducer=REDUCE_NONE
  &aggregation.perSeriesAligner=ALIGN_SUM
  &aggregation.alignmentPeriod={alignment_seconds}s
  &interval.startTime={RFC3339_START}
  &interval.endTime={RFC3339_END}
```

---

## 📦 Prerequisites & Dependencies (Zero Assumptions Guide)

If you are setting this up for the first time, you need:

1. **A Google Cloud Platform (GCP) Project** where your Google SecOps instance is bound (referred to as `SECOPS_PROJECT_ID`).
2. **Google Cloud CLI (`gcloud`)** installed on your administrative workstation ([Installation Guide](https://cloud.google.com/sdk/docs/install)).
3. **Python 3.9+** (if running natively) OR **Docker 20.10+** (if running via containers).
4. **Network Egress**: HTTPS outbound connectivity (TCP port 443) to `https://monitoring.googleapis.com`.

---

## 🔐 IAM & Service Account Setup (Least Privilege)

To pull metrics from Google Cloud Monitoring, the application needs an identity authorized to read monitoring data. Follow these step-by-step instructions to create a dedicated, least-privilege service account.

### Step 1: Enable Cloud Monitoring API

Ensure the Cloud Monitoring API is active on your SecOps Google Cloud project:

```bash
gcloud services enable monitoring.googleapis.com \
  --project="your-secops-project-id"
```

### Step 2: Create a Dedicated Service Account

Create a workload-specific service account. Do **not** use default Compute Engine or App Engine service accounts:

```bash
export SECOPS_PROJECT_ID="your-secops-project-id"
export SA_NAME="secops-ingest-monitor-sa"
export SA_EMAIL="${SA_NAME}@${SECOPS_PROJECT_ID}.iam.gserviceaccount.com"

gcloud iam service-accounts create "${SA_NAME}" \
  --project="${SECOPS_PROJECT_ID}" \
  --display-name="SecOps Ingestion Monitor Service Account" \
  --description="Read-only identity for querying Chronicle ingestion metrics from Cloud Monitoring"
```

### Step 3: Grant Least-Privilege IAM Roles

Grant **only** the `roles/monitoring.viewer` role to this service account on the target project. 

> [!IMPORTANT]
> **Least Privilege Principle**:
> - **DO NOT** grant primitive roles (`roles/viewer`, `roles/editor`, or `roles/owner`).
> - `roles/monitoring.viewer` (`Monitoring Viewer`) grants read-only access to time series metrics, metric descriptors, and monitoring groups. It cannot read logs, modify alerts, or access underlying Cloud Storage / BigQuery data.

```bash
gcloud projects add-iam-policy-binding "${SECOPS_PROJECT_ID}" \
  --member="serviceAccount:${SA_EMAIL}" \
  --role="roles/monitoring.viewer"
```

### Step 4: Provision Credentials

Depending on where you run the tool, select **one** of the following authentication strategies:

#### Option A: Running on Google Cloud (Compute Engine, GKE, or Cloud Run) — *Recommended (Keyless)*
Attach the service account directly to the VM or workload. The Google Cloud SDK and Python libraries will automatically fetch rotating OAuth2 JWT tokens from the internal metadata server (`http://metadata.google.internal`). No credential files or private keys are created or stored.

- **Compute Engine VM**:
  ```bash
  gcloud compute instances set-service-account INSTANCE_NAME \
    --zone=ZONE \
    --service-account="${SA_EMAIL}" \
    --scopes="cloud-platform"
  ```

#### Option B: Running Outside GCP (On-Premises, Laptop, or External Cloud) — *Service Account Key*
Generate a JSON credential key file:

```bash
gcloud iam service-accounts keys create credentials.json \
  --iam-account="${SA_EMAIL}" \
  --project="${SECOPS_PROJECT_ID}"
```

> [!WARNING]
> Keep `credentials.json` secure. Never commit it to git. The `.gitignore` file in this repository is already configured to block `*.json` credential files.

#### Option C: Local Developer Workstation (Application Default Credentials)
If you already log in with your corporate Google identity via `gcloud`:

```bash
gcloud auth application-default login
```

---

## ⚙️ Environment Variables & Configuration

The application can be fully configured via environment variables:

| Variable | Type | Default | Description |
| :--- | :---: | :---: | :--- |
| `SECOPS_PROJECT_ID` | `string` | `secops-superweird` | The GCP Project ID containing the SecOps Cloud Monitoring metrics. |
| `GOOGLE_APPLICATION_CREDENTIALS` | `string` | *None* | Path to the service account JSON key file (required for Option B). |
| `PORT` | `integer` | `8080` | Port on which the HTTP/HTTPS web server listens. |
| `SSL_ENABLED` | `boolean` | `false` | Enables HTTPS termination directly in the application (`true`/`false`). |
| `SSL_CERT_PATH` | `string` | `certs/cert.pem` | Path to public SSL certificate file (PEM format). |
| `SSL_KEY_PATH` | `string` | `certs/key.pem` | Path to private SSL key file (PEM format). |
| `MOCK_MODE` | `boolean` | `false` | When `true`, serves synthetic demo data without making GCP API calls. |

---

## 🚀 Installation & Quick Start

### Method 1: Local Python (Native)

1. **Clone the repository**:
   ```bash
   git clone https://github.com/your-username/google-secops-ingestion-tracking.git
   cd google-secops-ingestion-tracking
   ```

2. **Create and activate a virtual environment**:
   ```bash
   python3 -m venv venv
   source venv/bin/activate
   ```

3. **Install dependencies**:
   ```bash
   pip install --upgrade pip
   pip install -r requirements.txt
   ```

4. **Launch the Web Dashboard**:
   ```bash
   export SECOPS_PROJECT_ID="your-secops-project-id"
   export GOOGLE_APPLICATION_CREDENTIALS="/path/to/credentials.json"
   export PORT=8080

   python3 app.py
   ```
   Open your browser to: `http://localhost:8080`

---

### Method 2: Docker Container

1. **Build the container image**:
   ```bash
   docker build -t secops-ingestion:latest .
   ```

2. **Run in Mock / Demo Mode (No GCP credentials required)**:
   ```bash
   docker run -d --name secops-ingest-demo \
     -p 8080:8080 \
     -e MOCK_MODE=true \
     secops-ingestion:latest
   ```

3. **Run in Live Mode with Service Account Key**:
   ```bash
   docker run -d --name secops-ingest-prod \
     -p 8080:8080 \
     -v $(pwd)/credentials.json:/app/credentials.json:ro \
     -e GOOGLE_APPLICATION_CREDENTIALS=/app/credentials.json \
     -e SECOPS_PROJECT_ID="your-secops-project-id" \
     -e MOCK_MODE=false \
     secops-ingestion:latest
   ```

---

### Method 3: Docker Compose

Create a `docker-compose.yml` file:

```yaml
version: '3.8'

services:
  secops-ingestion:
    build: .
    container_name: secops-ingestion-monitor
    restart: unless-stopped
    ports:
      - "8080:8080"
    environment:
      - SECOPS_PROJECT_ID=your-secops-project-id
      - GOOGLE_APPLICATION_CREDENTIALS=/app/credentials.json
      - MOCK_MODE=false
      - PORT=8080
    volumes:
      - ./credentials.json:/app/credentials.json:ro
```

Run:
```bash
docker compose up -d
```

---

### Method 4: CLI Reporting & Exports

You can generate reports directly in your terminal or export them to CSV, JSON, or Markdown:

```bash
# Display daily summary with 30m reconciliation
./secops_ingestion_cli.py --project your-secops-project-id --timeframe daily --reconcile-30m

# Display weekly summary
./secops_ingestion_cli.py --project your-secops-project-id --timeframe weekly

# Display full 12-month summary
./secops_ingestion_cli.py --project your-secops-project-id --timeframe yearly

# Export report to files
./secops_ingestion_cli.py \
  --project your-secops-project-id \
  --timeframe daily \
  --export-csv report.csv \
  --export-json report.json \
  --export-md report.md

# Run offline in mock mode
./secops_ingestion_cli.py --mock --timeframe daily --reconcile-30m
```

---

## ⏱️ Automated Telemetry Collection & Scheduling

The web application provides a dedicated collection endpoint:

```http
POST /api/collect
```

When invoked, the server queries Cloud Monitoring for all 4 periods (`daily`, `weekly`, `monthly`, `yearly`), calculates reconciliations, and updates its in-memory cache.

### Automated Hourly Cron Setup

To schedule automated hourly metrics collection on a Linux host or VM:

```bash
# Open crontab editor
crontab -e

# Add an hourly job:
0 * * * * curl -s -X POST http://localhost:8080/api/collect > /dev/null 2>&1
```

### Google Cloud Scheduler Setup (Optional)

If running behind a Google Cloud Load Balancer or public domain:

```bash
gcloud scheduler jobs create http secops-hourly-collector \
  --location="us-central1" \
  --schedule="0 * * * *" \
  --time-zone="UTC" \
  --uri="https://your-domain.com/api/collect" \
  --http-method="POST"
```

---

## 🌐 REST API Reference

| Endpoint | Method | Query Parameters | Description |
| :--- | :---: | :--- | :--- |
| `/api/status` | `GET` | *None* | Connection mode (`live`/`mock`), project ID (redacted), and auth diagnostics. |
| `/api/ingestion/summary` | `GET` | `period` (`daily`, `weekly`, `monthly`, `yearly`) | High-level KPI totals (Bytes, Records, Normalized UDM, Error counts). |
| `/api/ingestion/breakdown` | `GET` | `period` | Detailed metrics broken down by `log_type` and `collector_id`. |
| `/api/ingestion/timeseries` | `GET` | `period` | Formatted timestamp points for Chart.js volume and records trend lines. |
| `/api/ingestion/reconcile` | `GET` | `period` | Side-by-side comparison of `sum(30m buckets)` vs `rollup` with official max. |
| `/api/ingestion/bigquery-compat` | `GET` | `period` | Telemetry formatted in legacy `datalake.ingestion_metrics` schema. |
| `/api/collect` | `POST` | *None* | Triggers a fresh telemetry extraction across all timeframes. |
| `/api/export/csv` | `GET` | `period` | Downloads the ingestion summary as a CSV file. |
| `/api/export/json` | `GET` | `period` | Downloads the ingestion summary as structured JSON. |
| `/api/refresh` | `POST` | *None* | Invalidates metric cache for on-demand UI refreshes. |

---

## 🧪 Testing & Quality Assurance

A comprehensive unit test suite is included in the `tests/` directory:

```bash
python3 -m unittest discover tests
```

### What the Tests Cover:
1. **Mathematical Accuracy**: Validates bytes-to-MB/GB conversions and normalization ratio calculations.
2. **Reconciliation Rules**: Asserts that `max(sum_30m, rollup)` is strictly honored under edge cases.
3. **BigQuery Schema Compatibility**: Verifies that delimiter `:RQ:` and field mappings match legacy BigQuery table schemas.
4. **Mock Mode Fallback**: Guarantees that the app runs completely offline without network or GCP dependencies.
5. **REST API Endpoints**: Tests HTTP response status codes, JSON structures, and input validation.

---

## 🔒 Security & Redaction

- **Least Privilege**: The application requires only `roles/monitoring.viewer`. It cannot modify infrastructure, view raw logs, or access proprietary security detections.
- **Tenant Redaction**: When displaying API queries in the web editor, tenant-specific project identifiers are sanitized to prevent accidental data leakage during demos or screenshots.
- **No Secret Hardcoding**: Credential paths and project IDs are injected exclusively via environment variables.

---

## 📝 Release Notes & Changelog

### Version 2.2.0 (September 2026)
- **High-Contrast Canvas Accessibility**:
  - Bound explicit `fontColor: '#f8fafc'` (slate-50) to all Chart.js legend items generated by `renderDonutChart()`. Resolves HTML5 Canvas 2D default black (`#000000`) text rendering against obsidian dark backgrounds when `fontColor` was omitted.
  - Set global high-contrast defaults (`Chart.defaults.color = '#f1f5f9'`) to prevent any unstyled chart labels or ticks from falling back to dark tones.
  - Enhanced doughnut chart hover tooltips to show formatted volume (`GB`/`MB`) and share percentage (`%`).
- **Telemetry Schema Alignment**:
  - Fixed `renderVolumeChart()` to parse `/api/ingestion/timeseries` timeline objects directly, restoring the spline line chart across all timeframes.
  - Updated `loadAllData()` to extract `breakData.log_types`, mapping normalized event counts, collector lists, and health statuses into the Log Sources Explorer and Top Log Sources doughnut chart.
  - Resolved 30-minute Rollup Reconciliation bindings (`sum_30m`, `rollup`, `official_max`, `winner`) and added empty-state feedback.
- **Port 443 & Native SSL**:
  - Added support for running directly on standard HTTPS port 443 with TLS certificates mounted at `/app/certs`.

### Version 2.1.0 (September 2026)
- **Cyber Bento Redesign**: Complete UI modernization using an asymmetric Bento Grid design system, glassmorphic obsidian cards, ambient aurora mesh styling, Tailwind CSS, and Alpine.js.
- **Self-Hosted Static Engine**: Replaced third-party CDN dependencies with local, self-hosted vendor bundles (`tailwind.min.js`, `alpine.min.js`, `chart.min.js`) for air-gapped and enterprise reliability.
- **Automated Telemetry Collection**: Added `/api/collect` endpoint for triggering scheduled background syncs via Google Cloud Scheduler or crontab.
- **Tenant Identifier Redaction**: Automatically redacts internal GCP project IDs from query inspectors and diagnostic endpoints.

---

## 📄 License

This project is licensed under the Apache 2.0 License.
