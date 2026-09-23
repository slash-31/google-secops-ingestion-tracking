# Google SecOps Ingestion Intelligence (Script & Site)

A production-ready CLI script and interactive web dashboard to pull, calculate, reconcile, and visualize daily, weekly, and monthly ingestion telemetry from **Google SecOps** via the **Google Cloud Monitoring API** (`v3 projects.timeSeries.list`).

---

## 🌟 Key Capabilities

1. **Native Cloud Monitoring v3 Metric Extraction**:
   - `chronicle.googleapis.com/ingestion/log/bytes_count` (Raw Ingestion Volume - Bytes, MB, GB, TB)
   - `chronicle.googleapis.com/ingestion/log/record_count` (Raw Ingested Records / Log Lines)
   - `chronicle.googleapis.com/normalizer/event/record_count` (Normalized UDM Events Produced)
2. **Flexible Timeframe Aggregations**:
   - **Daily** (Last 24 hours / 30m buckets)
   - **Weekly** (Last 7 days / 2h buckets)
   - **Monthly** (Last 30 days / 6h buckets)
3. **Customer Rollup Reconciliation Algorithm**:
   - Evaluates the sum of all 30-minute intervals (`*/30m` dataset) vs the whole-window rollup point.
   - Enforces `max(sum_30m, rollup)` as the official authoritative record for tracking, alerting, and capacity planning.
4. **Historical BigQuery Datalake Compatibility**:
   - Replicates the schema of the deprecated `chronicle-{project}.datalake.ingestion_metrics` table (migrated May 2025):
     - `log_type`
     - `collector_ids` (delimited with `:RQ:`)
     - `size_mb` / `size_gb`
     - `event_count` / `normalized_events` / `error_events`
     - `drop_reason_codes`
5. **Interactive Web Dashboard & REST API**:
   - Modern Google Cloud / SecOps dark-themed UI.
   - Live KPI cards, Chart.js time-series trend lines, and volume-share donut charts.
   - Searchable, filterable log sources breakdown table.
   - Dynamic reconciliation audit table and BigQuery schema side-by-side view.
   - Health and parser alert center.
   - 100% offline-compatible (bundled local JS/CSS).
6. **Dual Mode Engine (GCP Live + Synthetic Demo Mode)**:
   - Queries Google Cloud Monitoring API using Application Default Credentials (ADC) or Service Account JSON key.
   - Gracefully falls back to high-fidelity synthetic demo telemetry (modeled after real-world SecOps log sources like Palo Alto, Windows Sysmon, Linux Sysmon, Meraki, GCP VPC Flow, Auditd, ESXi) if offline or running in test environments.

---

## 🚀 Quick Start

### 1. Run the CLI Tool

#### Daily Ingestion Report with 30m Rollup Reconciliation:
```bash
./secops_ingestion_cli.py --project secops-superweird --timeframe daily --reconcile-30m
```

#### Weekly & Monthly Ingestion Reports:
```bash
# Weekly (Last 7 Days)
./secops_ingestion_cli.py --project secops-superweird --timeframe weekly

# All Timeframes (Daily, Weekly, Monthly)
./secops_ingestion_cli.py --project secops-superweird --timeframe all
```

#### Export to CSV, JSON, or Markdown:
```bash
./secops_ingestion_cli.py \
  --project secops-superweird \
  --timeframe daily \
  --export-csv daily_ingestion.csv \
  --export-json daily_ingestion.json \
  --export-md daily_report.md
```

#### Testing / Demo Mode (No GCP credentials required):
```bash
./secops_ingestion_cli.py --mock --timeframe daily --reconcile-30m
```

---

### 2. Launch the Web Dashboard Site

You can start the web dashboard directly using either command:

```bash
# Via CLI flag
./secops_ingestion_cli.py --serve --port 5000

# Or directly with Python
python3 app.py
```

Then open your browser to: **`http://localhost:5000`**

---

## 🛠️ Customer Query Pattern & Architecture

The Cloud Monitoring API query executed for each metric is:

```http
GET https://monitoring.googleapis.com/v3/projects/{project_id}/timeSeries?
  filter=metric.type = "chronicle.googleapis.com/{metric_type}"
  &aggregation.groupByFields=resource.labels.log_type, resource.labels.collector_id
  &aggregation.crossSeriesReducer=REDUCE_NONE
  &aggregation.perSeriesAligner=ALIGN_SUM
  &aggregation.alignmentPeriod={alignment_seconds}s
  &interval.startTime={RFC3339_START}
  &interval.endTime={RFC3339_END}
  &orderBy=resource.labels.log_type
  &pageSize=10000
```

### The 30m Reconciliation Logic:
```python
# For each log_type and collector_id:
val_30m = sum(pt.value for pt in series_30m)
val_rollup = sum(pt.value for pt in series_rollup)

# Enforce official record
official_record = max(val_30m, val_rollup)
```

---

## 📊 Deprecated BigQuery vs Cloud Monitoring Mapping

| BigQuery Field (`datalake.ingestion_metrics`) | Cloud Monitoring API Equivalent | Notes |
| :--- | :--- | :--- |
| `log_type` | `resource.labels.log_type` | Grouped in query |
| `collector_ids` | `resource.labels.collector_id` | Aggregated with `:RQ:` delimiter |
| `size_mb` | `chronicle.googleapis.com/ingestion/log/bytes_count` | `bytes / (1000 * 1000)` |
| `event_count` | `chronicle.googleapis.com/ingestion/log/record_count` | Raw logs received |
| `normalized_events` | `chronicle.googleapis.com/normalizer/event/record_count` | Validated UDM events |
| `error_events` | `record_count - normalized_events` | Unparsed / validation failures |
| `drop_reason_code` | Health status indicator | Flags parser syntax errors |

---

## 🌐 REST API Endpoints

The Flask application exposes a complete REST API:

| Endpoint | Method | Description |
| :--- | :---: | :--- |
| `/api/status` | GET | Connection mode (Live/Mock), GCP project ID, and auth diagnostics |
| `/api/ingestion/summary?period=daily\|weekly\|monthly` | GET | Ingestion totals (Bytes, Records, Normalized UDM, Health counts) |
| `/api/ingestion/breakdown?period=daily\|weekly\|monthly` | GET | Detailed breakdown by Log Type and Collector IDs |
| `/api/ingestion/timeseries?period=daily\|weekly\|monthly` | GET | Time-series data points for volume and records trends |
| `/api/ingestion/reconcile?period=daily\|weekly\|monthly` | GET | Customer 30m sum vs rollup comparison with official max record |
| `/api/ingestion/bigquery-compat?period=daily\|weekly\|monthly` | GET | Formatted in the legacy BigQuery table schema |
| `/api/export/csv?period=daily\|weekly\|monthly` | GET | Download summary CSV |
| `/api/export/json?period=daily\|weekly\|monthly` | GET | Download summary JSON |
| `/api/settings` | POST | Dynamically update Project ID, Credentials file, or Mock mode |
| `/api/refresh` | POST | Invalidate metric cache and trigger live query |

---

## 🧪 Running Automated Tests

```bash
python3 -m unittest discover tests
```
All 13 test suites verify metric math, derived fields, 30m reconciliation, BigQuery mapping, and REST API endpoints.
