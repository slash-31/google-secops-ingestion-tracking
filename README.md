# Google SecOps Ingestion Intelligence (CLI & Cyber Bento Dashboard)

[![Python 3.9+](https://img.shields.io/badge/python-3.9+-blue.svg)](https://www.python.org/downloads/)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](https://opensource.org/licenses/Apache-2.0)
[![Docker](https://img.shields.io/badge/docker-ready-blue.svg)](https://www.docker.com/)

A standalone CLI tool and interactive web dashboard that pulls, calculates, reconciles, and visualizes daily, weekly, monthly, and 12-month log ingestion telemetry from **Google SecOps (formerly Chronicle)** via the **Google Cloud Monitoring API v3** (`projects.timeSeries.list`), correlated side-by-side with **BindPlane Cloud** collector health and delivery ratios.

---

## 📑 Table of Contents

1. [Overview & Why This Exists](#-overview--why-this-exists)
2. [Key Capabilities](#-key-capabilities)
3. [Architecture & Data Flow](#-architecture--data-flow)
4. [Prerequisites (Zero-Assumptions Checklist)](#-prerequisites-zero-assumptions-checklist)
5. [Step-by-Step Credential & API Key Setup](#-step-by-step-credential--api-key-setup)
   - [Part A: Google Cloud Service Account & IAM (Least Privilege)](#part-a-google-cloud-service-account--iam-least-privilege)
   - [Part B: BindPlane Cloud Account ID & API Key (Optional)](#part-b-bindplane-cloud-account-id--api-key-optional)
   - [Part C: Generating Application Security Tokens (`ADMIN_SECRET` & `CRON_SECRET`)](#part-c-generating-application-security-tokens-admin_secret--cron_secret)
6. [Configuring Your `.env` File (No Secret Manager Required)](#-configuring-your-env-file-no-secret-manager-required)
   - [Complete Environment Variable Reference](#complete-environment-variable-reference)
7. [Installation & Running the Application](#-installation--running-the-application)
   - [Method 1: Local Python (Native)](#method-1-local-python-native)
   - [Method 2: Docker Container (`--env-file .env`)](#method-2-docker-container---env-file-env)
   - [Method 3: Docker Compose (`env_file: .env`)](#method-3-docker-compose-env_file-env)
   - [Method 4: CLI Reporting & File Exports](#method-4-cli-reporting--file-exports)
8. [BindPlane Pipeline Health & Delivery Reconciliation](#-bindplane-pipeline-health--delivery-reconciliation)
9. [Automated Telemetry Collection & Cron Scheduling](#-automated-telemetry-collection--cron-scheduling)
10. [REST API Reference](#-rest-api-reference)
11. [Testing & Quality Assurance](#-testing--quality-assurance)
12. [Security & Redaction Controls](#-security--redaction-controls)
13. [Release Notes & Changelog](#-release-notes--changelog)

---

## 💡 Overview & Why This Exists

Google SecOps publishes ingestion telemetry directly to **Google Cloud Monitoring API v3** time-series streams (`chronicle.googleapis.com/*`), replacing the retired legacy BigQuery `datalake.ingestion_metrics` export.

Security operations teams and detection engineers need a single pane of glass to answer:
1. **Raw Ingestion Volume**: How many gigabytes/terabytes of logs are ingested per log type (`PAN_FIREWALL`, `WINEVTLOG`, `LINUX_SYSMON`, `GCP_CLOUDAUDIT`, etc.) across 24h, 7d, 30d, and 12-month windows?
2. **Parser Health & Normalization**: How many raw log lines normalize into Unified Data Model (UDM) events versus drop due to parser errors?
3. **Collector & Delivery Health**: Are BindPlane collectors connected, are any sources silent or dropping events, and is Google SecOps actually receiving the bytes that BindPlane reports sending?
4. **30-Minute Rollup Reconciliation**: How does the sum of 30-minute aligned buckets compare against full-window rollups (`max(sum_30m, rollup)`)?

This project runs **anywhere** (laptop, Linux VM, Docker, or Cloud Run) and loads all configuration, API keys, secrets, and tokens directly from a local **`.env` file**—requiring **zero cloud secret managers or external databases**.

---

## 🌟 Key Capabilities

- **Unified Side-by-Side View (`BindPlane | Delivery | SecOps`)**: Joins BindPlane collector throughput, BindPlane-to-SecOps delivery ratios, and SecOps Cloud Monitoring ingestion/parsing metrics into one row per log type with expandable per-collector drill-downs and 24-hour sparklines.
- **Native Cloud Monitoring v3 Extraction**: Queries `projects.timeSeries.list` for:
  - `chronicle.googleapis.com/ingestion/log/bytes_count` (Raw Ingestion Bytes, MB, GB, TB)
  - `chronicle.googleapis.com/ingestion/log/record_count` (Raw Ingested Log Records)
  - `chronicle.googleapis.com/normalizer/event/record_count` (Normalized UDM Events Produced)
- **Multi-Horizon Timeframe Aggregations**:
  - **Daily**: Last 24 hours (30-minute alignment)
  - **Weekly**: Last 7 days (2-hour alignment)
  - **Monthly**: Last 30 days (6-hour alignment)
  - **12 Months / Yearly**: Last 365 days (24-hour alignment)
- **Authoritative 30m Reconciliation**: Computes `max(sum_30m, rollup)` per `(log_type, collector_id)` pair.
- **Legacy BigQuery Schema Compatibility**: Formats metrics into the legacy `datalake.ingestion_metrics` table structure (`:RQ:` collector delimiter) for drop-in pipeline compatibility.
- **Built-In Synthetic / Mock Mode (`MOCK_MODE=true`)**: Ships with a complete synthetic telemetry generator for both Google SecOps and BindPlane so you can evaluate or demo the dashboard immediately with zero credentials.

---

## 📐 Architecture & Data Flow

```
┌──────────────────────────────────┐       ┌──────────────────────────────────┐
│         BindPlane Cloud          │       │          Google SecOps           │
│  - /v1/agents                    │       │  (Forwarders, Feeds, Exporters)  │
│  - /v1/configurations            │       └─────────────────┬────────────────┘
│  - /v1/graphql (agentMetrics)    │                         │ Publishes metrics
└─────────────────┬────────────────┘                         ▼
                  │ HTTPS (API Key +              ┌──────────────────────────────────┐
                  │        Account ID)            │ Google Cloud Monitoring API v3   │
                  │                               │  - ingestion/log/bytes_count     │
                  │                               │  - ingestion/log/record_count    │
                  │                               │  - normalizer/event/record_count │
                  │                               └──────────┬───────────────────────┘
                  │                                          │ HTTPS (OAuth2 / SA Key)
                  ▼                                          ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│                 SecOps Ingestion Intelligence Suite (This App)               │
│                 Configured via local `.env` file (No Secret Manager)         │
│                                                                              │
│  ┌──────────────────────┐  ┌──────────────────────┐  ┌────────────────────┐  │
│  │ Unified & Delivery   │  │ 30m vs Rollup        │  │ CLI & CSV/JSON/MD  │  │
│  │ Reconciliation Engine│  │ Calculator           │  │ Exporter           │  │
│  └──────────────────────┘  └──────────────────────┘  └────────────────────┘  │
└──────────────────────────────────────────────────────────────────────────────┘
```

---

## 📦 Prerequisites (Zero-Assumptions Checklist)

Before installing, verify that you have the following tools installed on your machine. If you do not have them, follow the links provided:

1. **Git** (to clone this repository):
   - Check if installed: `git --version`
   - Install guide: [https://git-scm.com/downloads](https://git-scm.com/downloads)
2. **Python 3.9 or newer** (if running natively without Docker) **OR Docker 20.10+** (if running in a container):
   - Check Python: `python3 --version` ([Install Python](https://www.python.org/downloads/))
   - Check Docker: `docker --version` ([Install Docker](https://docs.docker.com/get-docker/))
3. **Google Cloud CLI (`gcloud`)** (used once during setup to create a read-only Service Account):
   - Check if installed: `gcloud --version`
   - Install guide: [https://cloud.google.com/sdk/docs/install](https://cloud.google.com/sdk/docs/install)
4. **A Google Cloud Project Bound to Google SecOps** (not required if you only want to run in `MOCK_MODE=true`):
   - You will need the **GCP Project ID** (a lowercase string like `acme-secops-prod-123`, **not** the numeric project number).
5. **Network Access**:
   - Outbound HTTPS (TCP port `443`) to `https://monitoring.googleapis.com` and `https://oauth2.googleapis.com`.
   - If using BindPlane integration: outbound HTTPS (TCP port `443`) to `https://app.bindplane.com` (or your self-hosted BindPlane URL).

---

## 🔐 Step-by-Step Credential & API Key Setup

> [!TIP]
> **Want to test the app first without any credentials?**
> Skip this section, copy `.env.example` to `.env`, set `MOCK_MODE="true"`, and jump straight to [Installation & Running the Application](#-installation--running-the-application).

---

### Part A: Google Cloud Service Account & IAM (Least Privilege)

To read SecOps metrics from Google Cloud Monitoring, the application needs a read-only Google Cloud identity. Follow these steps in your terminal:

#### Step 1: Log in to `gcloud` and set your SecOps GCP Project ID
Replace `your-secops-project-id` with your actual GCP Project ID:

```bash
gcloud auth login
export SECOPS_PROJECT_ID="your-secops-project-id"
gcloud config set project "${SECOPS_PROJECT_ID}"
```

#### Step 2: Enable the Google Cloud Monitoring API
```bash
gcloud services enable monitoring.googleapis.com --project="${SECOPS_PROJECT_ID}"
```

#### Step 3: Create a Dedicated Service Account
```bash
export SA_NAME="secops-ingest-monitor-sa"
export SA_EMAIL="${SA_NAME}@${SECOPS_PROJECT_ID}.iam.gserviceaccount.com"

gcloud iam service-accounts create "${SA_NAME}" \
  --project="${SECOPS_PROJECT_ID}" \
  --display-name="SecOps Ingestion Monitor Service Account" \
  --description="Read-only service account for querying SecOps metrics from Cloud Monitoring"
```

#### Step 4: Grant Only `roles/monitoring.viewer` (Least Privilege)
> [!IMPORTANT]
> Do **not** grant `roles/owner`, `roles/editor`, or `roles/viewer`. The `roles/monitoring.viewer` role grants read-only access strictly to Cloud Monitoring time-series metrics.

```bash
gcloud projects add-iam-policy-binding "${SECOPS_PROJECT_ID}" \
  --member="serviceAccount:${SA_EMAIL}" \
  --role="roles/monitoring.viewer"
```

#### Step 5: Download the Service Account JSON Key File (or use ADC)
Choose **one** of the following methods to provide credentials to the app via `.env`:

- **Option A1 — Service Account JSON File (Standard for VMs, Docker, and Servers):**
  Generate a JSON key file and restrict its permissions:
  ```bash
  mkdir -p ~/.creds
  gcloud iam service-accounts keys create ~/.creds/secops-sa.json \
    --iam-account="${SA_EMAIL}" \
    --project="${SECOPS_PROJECT_ID}"
  chmod 600 ~/.creds/secops-sa.json
  ```
  You will set `GOOGLE_APPLICATION_CREDENTIALS="/Users/yourname/.creds/secops-sa.json"` in your `.env` file.

- **Option A2 — Short-Lived Bearer Token (`GCP_ACCESS_TOKEN`):**
  If your organization blocks service account key creation (`constraints/iam.disableServiceAccountKeyCreation`), you can generate a temporary bearer token and place it in `.env`:
  ```bash
  gcloud auth print-access-token
  ```
  You will set `GCP_ACCESS_TOKEN="ya29.a0..."` in your `.env` file, or run `gcloud auth application-default login` and leave both `GOOGLE_APPLICATION_CREDENTIALS` and `GCP_ACCESS_TOKEN` empty in `.env`.

---

### Part B: BindPlane Cloud Account ID & API Key (Optional)

If you use **BindPlane Cloud** (or a self-hosted BindPlane OPAMP server) to forward logs to Google SecOps, configuring BindPlane credentials in `.env` unlocks the **Unified by Log Type**, **Delivery Reconciliation**, **Pipeline Issues**, and **BindPlane Collectors** views.

> [!NOTE]
> BindPlane's API requires **both** your **Account ID** (`X-Bindplane-Account-ID`) and your **API Key** (`X-Bindplane-Api-Key`) on every request. An API key alone cannot auto-discover its project/account.

1. **Find your `BINDPLANE_ACCOUNT_ID` (26-character ULID)**:
   - Log in to [https://app.bindplane.com](https://app.bindplane.com).
   - Click **Settings** (gear icon) or your **Project Name** in the top navigation bar.
   - Copy the **Account ID / Project ID** (a 26-character uppercase alphanumeric ULID such as `01ARZ3NDEKTSV4RRFFQ69G5FAV`).
2. **Generate your `BINDPLANE_API_KEY`**:
   - In BindPlane, go to **Settings / Profile → API Keys** (or **Service Accounts**).
   - Click **Create API Key**, give it a name (e.g., `secops-ingestion-dashboard`), and copy the generated key string.
3. **Add both values to `.env`**:
   - Set `BINDPLANE_ACCOUNT_ID="01XXXXXXXXXXXXXXXXXXXXXXXX"`
   - Set `BINDPLANE_API_KEY="your-bindplane-api-key"` (or save the key in a local file with `chmod 600` and set `BINDPLANE_API_KEY_FILE="/path/to/bindplane-key.txt"` in `.env`).

---

### Part C: Generating Application Security Tokens (`ADMIN_SECRET` & `CRON_SECRET`)

To prevent unauthorized callers or web crawlers from mutating settings or triggering expensive Cloud Monitoring API syncs, the application protects its `POST` endpoints using two tokens stored in `.env`:
- `ADMIN_SECRET`: Required to call `POST /api/settings` and `POST /api/refresh`.
- `CRON_SECRET`: Required to call `POST /api/collect`.

Generate two random 32-byte hex tokens in your terminal:

```bash
# Generate ADMIN_SECRET
openssl rand -hex 32

# Generate CRON_SECRET
openssl rand -hex 32
```

Copy both outputs into your `.env` file.

---

## ⚙️ Configuring Your `.env` File (No Secret Manager Required)

All configuration—including project IDs, credential paths, API keys, and security tokens—is managed through a single `.env` file in the root of this repository. **Both `app.py`, `secops_ingestion_cli.py`, and Gunicorn automatically load `.env` on startup.**

### Step 1: Copy the included template
```bash
cp .env.example .env
chmod 600 .env
```

### Step 2: Edit `.env` with your values
Open `.env` in any text editor (`nano .env`, `vim .env`, or VS Code) and replace the placeholders:

```ini
# ==============================================================================
# 1. GOOGLE SECOPS / CLOUD MONITORING CONFIGURATION
# ==============================================================================
SECOPS_PROJECT_ID="your-secops-project-id"

# Path to your Service Account JSON file (leave empty if using ADC or GCP_ACCESS_TOKEN)
GOOGLE_APPLICATION_CREDENTIALS="/path/to/your/credentials.json"

# Optional: Short-lived OAuth2 bearer token (leave empty if using JSON key file above)
GCP_ACCESS_TOKEN=""

# Set to "true" for synthetic demo data, or "false" for live Google Cloud data
MOCK_MODE="false"
ALLOW_MOCK_FALLBACK="false"

# ==============================================================================
# 2. BINDPLANE CLOUD / OPAMP COLLECTOR TELEMETRY (OPTIONAL)
# ==============================================================================
# 26-character ULID from BindPlane Project Settings
BINDPLANE_ACCOUNT_ID="01XXXXXXXXXXXXXXXXXXXXXXXX"

# Raw BindPlane API key (or leave blank and use BINDPLANE_API_KEY_FILE below)
BINDPLANE_API_KEY="bp_api_key_replace_with_your_actual_key"
BINDPLANE_API_KEY_FILE=""

BINDPLANE_BASE_URL="https://app.bindplane.com"
BINDPLANE_SECOPS_COLLECTOR_IDS="aaaa1111-aaaa-1111-aaaa-1111aaaa1112"

# ==============================================================================
# 3. APPLICATION SECURITY TOKENS (ADMIN & CRON ENDPOINTS)
# ==============================================================================
# Generate each with: openssl rand -hex 32
ADMIN_SECRET="replace-with-random-hex-token-from-openssl-rand-hex-32"
CRON_SECRET="replace-with-random-hex-token-from-openssl-rand-hex-32"

# ==============================================================================
# 4. WEB SERVER & SSL/TLS SETTINGS
# ==============================================================================
HOST="0.0.0.0"
PORT="8080"
SSL_ENABLED="false"
SSL_CERT_PATH="certs/cert.pem"
SSL_KEY_PATH="certs/key.pem"
```

> [!WARNING]
> **Never commit `.env` to Git.** The `.gitignore` and `.dockerignore` files in this repository are pre-configured to ignore `.env`, `.env.*`, `credentials.json`, and `*.key` files while keeping `.env.example` tracked as a safe template.

### Complete Environment Variable Reference

| Variable | Type | Default | Description |
| :--- | :---: | :---: | :--- |
| `SECOPS_PROJECT_ID` | `string` | `your-secops-project-id` | GCP Project ID where Google SecOps metrics are stored. |
| `GOOGLE_APPLICATION_CREDENTIALS` | `string` | *None* | Path to the GCP Service Account JSON key file. |
| `GCP_ACCESS_TOKEN` | `string` | *None* | Optional raw OAuth2 bearer token for Cloud Monitoring API. |
| `MOCK_MODE` | `boolean` | `false` | When `true`, serves synthetic demo data without live API calls. |
| `ALLOW_MOCK_FALLBACK` | `boolean` | `false` | When `false`, Cloud Monitoring auth/API failures return HTTP 503 instead of fabricating data. |
| `BINDPLANE_ACCOUNT_ID` | `string` | *None* | BindPlane Cloud project/account ID (26-character ULID). |
| `BINDPLANE_API_KEY` | `string` | *None* | BindPlane API key loaded directly from `.env`. |
| `BINDPLANE_API_KEY_FILE` | `string` | *None* | Optional path to a file containing the BindPlane API key. |
| `BINDPLANE_BASE_URL` | `string` | `https://app.bindplane.com` | BindPlane server base URL (`https://` required). |
| `BINDPLANE_SECOPS_COLLECTOR_IDS` | `csv` | `aaaa1111-aaaa-1111-aaaa-1111aaaa1112` | SecOps `collector_id`(s) identifying BindPlane Chronicle exporter traffic. |
| `ADMIN_SECRET` | `string` | *None* | Shared secret required for `POST /api/settings` and `POST /api/refresh`. |
| `CRON_SECRET` | `string` | *None* | Shared secret required for `POST /api/collect`. |
| `HOST` | `string` | `0.0.0.0` | Listen address (`127.0.0.1` for local-only access). |
| `PORT` | `integer` | `8080` / `8443` | HTTP/HTTPS listener port. |
| `SSL_ENABLED` | `boolean` | `false` | Enables direct HTTPS termination using `SSL_CERT_PATH` and `SSL_KEY_PATH`. |
| `SSL_CERT_PATH` | `string` | `certs/cert.pem` | Path to PEM-encoded TLS certificate. |
| `SSL_KEY_PATH` | `string` | `certs/key.pem` | Path to PEM-encoded TLS private key. |
| `PIPELINE_DROP_WARN_PCT` / `PIPELINE_DROP_CRIT_PCT` | `float` | `20` / `50` | Collector source-to-destination drop % for warning / critical alerts. |
| `PIPELINE_DROP_MIN_INPUT_BPS` | `float` | `100` | Minimum collector input bytes/sec before evaluating drop %. |
| `PIPELINE_SILENT_BPS` | `float` | `0` | Throughput (B/s) at or below which a collector/source is flagged silent. |
| `SECOPS_SILENT_AFTER_MINUTES` | `integer` | `90` | Floor in minutes before flagging a SecOps log type as silent. |
| `SECOPS_SILENT_CADENCE_MULTIPLIER` | `float` | `3` | Multiplier of median arrival interval used for cadence-aware silence detection. |
| `SECOPS_SILENT_MIN_BUCKETS` | `integer` | `4` | Minimum non-zero 30m buckets in 24h required to evaluate cadence. |
| `SECOPS_SILENT_IGNORE_LOG_TYPES` | `csv` | *None* | Comma-separated log types excluded from silence alerts. |
| `DELIVERY_GAP_WARN_PCT` / `DELIVERY_GAP_CRIT_PCT` | `float` | `50` / `80` | Recent vs. 24h SecOps/BindPlane ratio drop % for warning / critical alerts. |
| `DELIVERY_BASELINE_OUTLIER_PCT` | `float` | `75` | 24h ratio outlier threshold below the fleet median ratio (%). |
| `DELIVERY_MIN_BYTES` | `float` | `1000000` | Minimum BindPlane window bytes (`1 MB`) required to evaluate delivery ratios. |

---

## 🚀 Installation & Running the Application

### Method 1: Local Python (Native)

1. **Clone the repository and enter the directory**:
   ```bash
   git clone https://github.com/slash-31/google-secops-ingestion-tracking.git
   cd google-secops-ingestion-tracking
   ```

2. **Create and activate a Python virtual environment**:
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   ```

3. **Install dependencies**:
   ```bash
   pip install --upgrade pip
   pip install -r requirements.txt
   ```

4. **Create your `.env` file from the template**:
   ```bash
   cp .env.example .env
   chmod 600 .env
   # Edit .env and fill in SECOPS_PROJECT_ID, GOOGLE_APPLICATION_CREDENTIALS, BINDPLANE_*, etc.
   ```

5. **Start the Web Dashboard**:
   ```bash
   python3 app.py
   ```
   Open your browser to `http://localhost:8080` (or `https://localhost:443` if `SSL_ENABLED="true"`).

---

### Method 2: Docker Container (`--env-file .env`)

1. **Build the Docker image**:
   ```bash
   docker build -t secops-ingestion:latest .
   ```

2. **Option A — Run in Offline Mock / Demo Mode (No credentials needed)**:
   ```bash
   docker run -d --name secops-ingest-demo \
     -p 8443:8443 \
     -e MOCK_MODE=true \
     secops-ingestion:latest
   ```
   Open `https://localhost:8443` in your browser.

3. **Option B — Run in Live Production Mode using `.env`**:
   Ensure your `.env` file has `GOOGLE_APPLICATION_CREDENTIALS=/app/credentials.json` and your `SECOPS_PROJECT_ID`, `BINDPLANE_ACCOUNT_ID`, `BINDPLANE_API_KEY`, `ADMIN_SECRET`, and `CRON_SECRET` populated, then run:
   ```bash
   docker run -d --name secops-ingest-prod \
     -p 443:8443 \
     --env-file .env \
     -v ~/.creds/secops-sa.json:/app/credentials.json:ro \
     secops-ingestion:latest
   ```
   Open `https://localhost` in your browser.

---

### Method 3: Docker Compose (`env_file: .env`)

Create a `docker-compose.yml` file in the project root:

```yaml
services:
  secops-ingestion:
    build: .
    container_name: secops-ingestion-monitor
    restart: unless-stopped
    ports:
      - "443:8443"
    env_file:
      - .env
    environment:
      - GOOGLE_APPLICATION_CREDENTIALS=/app/credentials.json
      - PORT=8443
    volumes:
      - ~/.creds/secops-sa.json:/app/credentials.json:ro
```

Start the stack:
```bash
docker compose up -d
```

---

### Method 4: CLI Reporting & File Exports

`secops_ingestion_cli.py` automatically loads `.env` as well, or accepts flags directly on the command line:

```bash
# Pull Daily metrics using settings from .env and run 30m rollup reconciliation
./secops_ingestion_cli.py --timeframe daily --reconcile-30m

# Pull Weekly, Monthly, or 12-Month (Yearly) metrics
./secops_ingestion_cli.py --timeframe weekly
./secops_ingestion_cli.py --timeframe monthly
./secops_ingestion_cli.py --timeframe yearly

# Export summary reports to CSV, JSON, and Markdown
./secops_ingestion_cli.py \
  --timeframe daily \
  --export-csv daily_report.csv \
  --export-json daily_report.json \
  --export-md daily_report.md

# Run completely offline with synthetic demo data
./secops_ingestion_cli.py --mock --timeframe daily --reconcile-30m
```

---

## 🩺 BindPlane Pipeline Health & Delivery Reconciliation

When `BINDPLANE_ACCOUNT_ID` and `BINDPLANE_API_KEY` (or `BINDPLANE_API_KEY_FILE`) are set in `.env` (or when `MOCK_MODE="true"`), the dashboard evaluates collector and delivery health every 60 seconds:

### Detection Rules

| Category | Severity | Trigger Condition |
| :--- | :---: | :--- |
| `AGENT_DISCONNECTED` | `critical` | Collector is disconnected from BindPlane. |
| `AGENT_SILENT` | `critical` | Collector is connected, but sent `0 B/s` to the SecOps destination in the window. |
| `SOURCE_SILENT` | `warning` | One source on an active collector is at `0 B/s` while other sources on the same collector are flowing. |
| `SOURCE_NO_DATA_24H` | `warning` | A configured source reported no throughput rows at all over 24 hours. |
| `PIPELINE_DROP` | `warning` / `critical` | Collector destination output is ≥20% (`warning`) or ≥50% (`critical`) below source input. |
| `AGENT_CONFIG_FAILED` | `critical` | OpAMP `remoteConfigStatus = FAILED` on the collector. |
| `CONFIG_ROLLOUT_ERROR` | `critical` | BindPlane configuration rollout reports agent errors. |
| `AGENT_UNHEALTHY` / `AGENT_STATUS_ABNORMAL` | `warning` | Non-zero `healthStatus` or abnormal agent `status`. |
| `AGENTS_OUT_OF_DATE` | `info` | Aggregate count of collectors with an agent upgrade available. |
| `SECOPS_LOG_TYPE_SILENT` | `critical` | A SecOps log type stops arriving for longer than `max(90 min, 3 × median arrival gap)`. |
| `SECOPS_PARSER_FAILURE` | `critical` | Raw records are arriving in SecOps, but `0` normalized UDM events are produced. |
| `SECOPS_NORMALIZATION_LOW` | `warning` | UDM normalization rate is below 75%. |
| `DELIVERY_STOPPED` | `critical` | BindPlane sent ≥1 MB of a log type in the last hour, but SecOps received `0` bytes. |
| `DELIVERY_GAP` | `warning` / `critical` | Recent 1h SecOps/BindPlane byte ratio dropped ≥50% (`warning`) or ≥80% (`critical`) below its 24h baseline. |
| `DELIVERY_GAP_24H` | `warning` | A log type's 24h SecOps/BindPlane ratio is ≥75% below the fleet-wide median ratio (identifies the top sending collector). |
| `LOG_TYPE_NOT_IN_SECOPS` | `warning` | BindPlane sent ≥1 MB of a log type over 24h that SecOps never recorded. |
| `BINDPLANE_API_UNAVAILABLE` / `SECOPS_TELEMETRY_UNAVAILABLE` | `critical` | One upstream API failed; the remaining upstream continues to be evaluated independently. |

---

## ⏱️ Automated Telemetry Collection & Cron Scheduling

The `POST /api/collect` endpoint triggers a full refresh across all 4 timeframes (`daily`, `weekly`, `monthly`, `yearly`). It requires the `CRON_SECRET` token configured in your `.env` file.

### Example `curl` Request Using `CRON_SECRET` from `.env`
```bash
source .env
curl -k -X POST https://localhost:8443/api/collect \
  -H "X-Cron-Token: ${CRON_SECRET}"
```

### Hourly Linux Crontab Example
```bash
crontab -e
```
Add:
```cron
0 * * * * . /path/to/google-secops-ingestion-tracking/.env && curl -ksS -X POST https://127.0.0.1:8443/api/collect -H "X-Cron-Token: ${CRON_SECRET}" > /dev/null
```

---

## 🌐 REST API Reference

| Endpoint | Method | Auth Header Required | Description |
| :--- | :---: | :---: | :--- |
| `/api/status` | `GET` | *None* | Connection status (`live`/`mock`), redacted project ID, and SSL status. |
| `/api/ingestion/summary` | `GET` | *None* | KPI totals for `period` (`daily`, `weekly`, `monthly`, `yearly`). |
| `/api/ingestion/breakdown` | `GET` | *None* | Per-`log_type` and `collector_id` breakdown for `period`. |
| `/api/ingestion/timeseries` | `GET` | *None* | Time-series data points for Chart.js trend lines. |
| `/api/ingestion/reconcile` | `GET` | *None* | 30-minute bucket sum vs. full-window rollup reconciliation. |
| `/api/ingestion/bigquery-compat` | `GET` | *None* | Legacy `datalake.ingestion_metrics` compatible rows. |
| `/api/pipeline/health` | `GET` | *None* | BindPlane + SecOps issues, collector fleet, delivery ratios, and Unified View (`window=5m\|1h\|24h`). |
| `/api/export/csv` | `GET` | *None* | Downloads CSV report (hardened against CWE-1236 formula injection). |
| `/api/export/json` | `GET` | *None* | Downloads JSON report with redacted project ID. |
| `/api/refresh` | `POST` | `X-Auth-Token: <ADMIN_SECRET>` or `Authorization: Bearer <ADMIN_SECRET>` | Clears cached telemetry to force an immediate reload. |
| `/api/settings` | `POST` | `X-Auth-Token: <ADMIN_SECRET>` or `Authorization: Bearer <ADMIN_SECRET>` | Updates runtime `project_id` or `mock_mode`. |
| `/api/collect` | `POST` | `X-Cron-Token: <CRON_SECRET>` or `Authorization: Bearer <CRON_SECRET>` | Refreshes and caches all 4 periods (`daily`, `weekly`, `monthly`, `yearly`). |

---

## 🧪 Testing & Quality Assurance

Run the full unit test suite (120+ tests):

```bash
python3 -m unittest discover tests
# or with pytest:
pip install -r requirements-dev.txt
PYTHONPATH=. pytest -v
```

---

## 🔒 Security & Redaction Controls

- **`.env`-First Secret Management**: All API keys, paths, and tokens are read from `.env` (or environment variables) and never hardcoded in source files.
- **Deny-by-Default Mutating Routes**: `/api/settings`, `/api/refresh`, and `/api/collect` refuse requests (`HTTP 503`) if `ADMIN_SECRET` / `CRON_SECRET` are not set in `.env`.
- **Automatic Tenant Redaction**: Project IDs are masked (`yo******` / `[REDACTED_PROJECT_ID]`) in public API responses and BigQuery compatibility views, and credential previews or filesystem paths are never returned in diagnostic responses.
- **Hardened HTTP Headers**: Enforces `Strict-Transport-Security`, `Content-Security-Policy` (`frame-ancestors 'none'`), `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Cache-Control: no-store`, and CSV formula-injection neutralization.

---

## 📝 Release Notes & Changelog

### Version 2.4.1 (October 2026)
- **Built-In `.env` Loader & `.env.example`**: Added automatic zero-dependency `.env` loading in `secops_ingestion/config.py` and a documented `.env.example` template with placeholders for all GCP, BindPlane, Admin, and Cron credentials.
- **Public Repository Sanitization**: Removed all environment-specific hostnames and internal project references from test fixtures and docstrings.
- **Cache-Control & Static Asset Versioning**: Added cache-busting query strings to static assets and `Cache-Control: no-store` on `/` for CDN compatibility.

### Version 2.4.0 (October 2026)
- **Unified View (`BindPlane | Delivery | SecOps`)**: Default first tab joining BindPlane collector metrics, delivery ratios, and SecOps Cloud Monitoring metrics into a single row per log type with expandable per-collector drill-downs.
- **Delivery Reconciliation**: BindPlane-to-SecOps comparison per log type (`DELIVERY_STOPPED`, `DELIVERY_GAP`, `DELIVERY_GAP_24H`, `LOG_TYPE_NOT_IN_SECOPS`).
- **Idle Source Detection**: `SOURCE_NO_DATA_24H` for configured sources with zero 24h measurements.

### Version 2.3.0 (October 2026)
- **BindPlane Pipeline Health**: Added `/api/pipeline/health` endpoint and UI panel for collector outages, silent collectors/sources, pipeline drops, rollout failures, cadence-aware SecOps silence detection, and parser failures.

### Version 2.2.0 (September 2026)
- **High-Contrast Canvas Accessibility & Native SSL**: Explicit light-slate legend colors on dark backgrounds and native port 443/8443 HTTPS support.

### Version 2.1.0 (September 2026)
- **Cyber Bento Redesign**: Glassmorphic dark UI with local self-hosted Tailwind CSS, Alpine.js, and Chart.js bundles.

---

## 📄 License

This project is licensed under the Apache 2.0 License.
