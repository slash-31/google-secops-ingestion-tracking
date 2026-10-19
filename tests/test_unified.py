"""
Tests for the unified BindPlane | Delivery | SecOps log-type view.
"""
import unittest
from datetime import datetime, timedelta, timezone

from secops_ingestion.calculator import LogSourceMetrics
from secops_ingestion.delivery import GroupTotals
from secops_ingestion.pipeline_health import Issue
from secops_ingestion.unified import (
    build_unified_rows,
    path_for_collector,
    secops_series_by_log_type,
)

BP = "aaaa1111-aaaa-1111-aaaa-1111aaaa1112"
GCP = "aaaa3333-aaaa-3333-aaaa-3333aaaa3333"
FEED = "dddddddd-dddd-dddd-dddd-dddddddddddd"
END = datetime(2026, 10, 19, 18, 0, tzinfo=timezone.utc)
SINCE, UNTIL = END - timedelta(minutes=90), END - timedelta(minutes=30)


def ts(minutes_ago):
    return (END - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def series(log_type, collector, points):
    return {"resource": {"labels": {"log_type": log_type, "collector_id": collector}},
            "points": [{"interval": {"endTime": ts(m)}, "value": {"int64Value": str(v)}} for m, v in points]}


def meta(log_type, nbytes, records, normalized):
    m = LogSourceMetrics(log_type=log_type, bytes_count=nbytes, record_count=records,
                         normalized_event_count=normalized)
    m.calculate_derived_fields()
    return m


def group_totals(by_agent, configs=("cfg",)):
    g = GroupTotals(bytes=sum(by_agent.values()), configurations=set(configs))
    g.bytes_by_agent = dict(by_agent)
    return g


def agent_row(name, health="ok", connected=True, config="cfg"):
    return {"name": name, "health": health, "connected": connected, "configuration": config}


class TestHelpers(unittest.TestCase):

    def test_path_for_collector(self):
        self.assertEqual(path_for_collector(BP, [BP]), "BindPlane")
        self.assertEqual(path_for_collector(GCP, [BP]), "GCP direct")
        self.assertEqual(path_for_collector(FEED, [BP]), "Feed / context")
        self.assertEqual(path_for_collector("custom-id", [BP]), "Other")
        self.assertEqual(path_for_collector("custom-id", ["custom-id"]), "BindPlane")

    def test_series_index(self):
        times, by_time, by_collector = secops_series_by_log_type([
            series("A", BP, [(30, 5), (0, 1)]),
            series("A", GCP, [(30, 2)]),
        ])
        self.assertEqual(times, [ts(30), ts(0)])  # chronological
        self.assertEqual(by_time["A"][ts(30)], 7.0)
        self.assertEqual(by_collector["A"], {BP: 6.0, GCP: 2.0})


class TestBuildUnifiedRows(unittest.TestCase):

    def build(self, issues=None, agents=None, delivery=None):
        groups_recent = {
            ("AUDITD",): group_totals({"host-a": 3600 * 100.0, "host-b": 0.0}),
            ("WINDOWS_SYSMON", "WINEVTLOG"): group_totals({"dc1": 3600 * 10.0}, ("win",)),
        }
        groups_baseline = {
            ("AUDITD",): group_totals({"host-a": 100.0, "host-b": 900.0}),
            ("WINDOWS_SYSMON", "WINEVTLOG"): group_totals({"dc1": 50.0}, ("win",)),
        }
        secops_series = [
            series("AUDITD", BP, [(60, 40), (30, 40), (0, 10)]),
            series("WINEVTLOG", BP, [(60, 3)]),
            series("WINDOWS_SYSMON", BP, [(60, 1)]),
            series("GCP_DNS", GCP, [(60, 20)]),
            series("EMPTY_TYPE", GCP, [(60, 0)]),
        ]
        log_types = [
            meta("AUDITD", 90, 100, 99), meta("WINEVTLOG", 3, 10, 10),
            meta("WINDOWS_SYSMON", 1, 5, 5), meta("GCP_DNS", 20, 50, 50),
            meta("EMPTY_TYPE", 0, 0, 0),
        ]
        return build_unified_rows(
            groups_recent=groups_recent,
            groups_baseline=groups_baseline,
            delivery_rows=delivery or [
                {"group": "AUDITD", "status": "gap_24h", "ratio_recent": 0.38, "ratio_baseline": 0.02, "ratio_change_pct": 1500.0},
                {"group": "WINDOWS_SYSMON + WINEVTLOG", "status": "ok", "ratio_recent": 0.4, "ratio_baseline": 0.4, "ratio_change_pct": 0.0},
            ],
            secops_bytes_series=secops_series,
            secops_log_types=log_types,
            agents_table=agents or [agent_row("host-a"), agent_row("host-b", health="critical"), agent_row("dc1", config="win")],
            issues=issues or [],
            bindplane_collector_ids=[BP],
            recent_since=SINCE,
            recent_until=UNTIL,
        )

    def row(self, rows, label):
        return next(r for r in rows if r["label"] == label)

    def test_rows_cover_bindplane_groups_and_secops_only_types(self):
        rows = self.build()
        labels = {r["label"] for r in rows}
        self.assertEqual(labels, {"AUDITD", "WINDOWS_SYSMON + WINEVTLOG", "GCP_DNS"})  # EMPTY_TYPE dropped
        self.assertIsNone(self.row(rows, "GCP_DNS")["bindplane"])
        self.assertEqual(self.row(rows, "GCP_DNS")["paths"], ["GCP direct"])

    def test_bindplane_columns_and_collector_drilldown(self):
        auditd = self.row(self.build(), "AUDITD")
        bp = auditd["bindplane"]
        self.assertEqual(bp["collector_count"], 2)
        self.assertEqual(bp["silent_collectors"], 1)
        self.assertEqual(bp["sent_bytes_recent"], 360000)
        # Critical collector sorts first; share is of the 24h group volume.
        self.assertEqual(bp["collectors"][0]["name"], "host-b")
        self.assertEqual(bp["collectors"][0]["share_24h_pct"], 90.0)
        self.assertEqual(bp["collectors"][1]["bps_recent"], 100.0)
        self.assertEqual(bp["status"], "critical")

    def test_secops_columns(self):
        auditd = self.row(self.build(), "AUDITD")["secops"]
        self.assertEqual(auditd["received_bytes_recent"], 80)  # buckets at 60 and 30 min, not the filling one
        self.assertEqual(auditd["bytes_24h"], 90)
        self.assertEqual(auditd["normalization_rate_pct"], 99.0)
        self.assertEqual(auditd["paths_bytes_24h"], {"BindPlane": 90})
        self.assertEqual(len(auditd["sparkline"]), 3)

    def test_multi_log_type_group_sums_secops(self):
        win = self.row(self.build(), "WINDOWS_SYSMON + WINEVTLOG")
        self.assertEqual(win["secops"]["bytes_24h"], 4)
        self.assertEqual(win["secops"]["records_24h"], 15)
        self.assertEqual([p["log_type"] for p in win["secops"]["per_log_type"]], ["WINDOWS_SYSMON", "WINEVTLOG"])

    def test_status_is_worst_of_all_sides_and_sorted(self):
        rows = self.build()
        self.assertEqual(rows[0]["label"], "AUDITD")
        self.assertEqual(rows[0]["status"], "critical")  # critical collector dominates gap_24h warning
        self.assertEqual(self.row(rows, "GCP_DNS")["status"], "ok")

    def test_delivery_only_on_bindplane_rows(self):
        rows = self.build()
        self.assertEqual(self.row(rows, "AUDITD")["delivery"]["status"], "gap_24h")
        self.assertIsNone(self.row(rows, "GCP_DNS")["delivery"])

    def test_issue_attachment_rules(self):
        issues = [
            Issue("critical", "SECOPS_LOG_TYPE_SILENT", "log_type", "GCP_DNS", "m"),
            Issue("warning", "DELIVERY_GAP_24H", "log_type", "AUDITD", "m"),
            Issue("critical", "AGENT_SILENT", "agent", "host-b", "m"),
            Issue("warning", "SOURCE_NO_DATA_24H", "agent", "dc1 / AUDITD", "m"),       # scoped to AUDITD row
            Issue("warning", "SOURCE_SILENT", "agent", "dc1 / source/source1_X", "m"),  # agent-wide
            Issue("info", "AGENTS_OUT_OF_DATE", "integration", "bindplane", "m"),
        ]
        rows = self.build(issues=issues)
        cats = lambda label: sorted(i["category"] for i in self.row(rows, label)["issues"])
        self.assertEqual(cats("AUDITD"), ["AGENT_SILENT", "DELIVERY_GAP_24H", "SOURCE_NO_DATA_24H"])
        self.assertEqual(cats("WINDOWS_SYSMON + WINEVTLOG"), ["SOURCE_SILENT"])
        self.assertEqual(cats("GCP_DNS"), ["SECOPS_LOG_TYPE_SILENT"])
        self.assertEqual(self.row(rows, "GCP_DNS")["status"], "critical")

    def test_secops_only_with_no_inputs(self):
        rows = build_unified_rows({}, {}, [], [], [], [], [], [BP], SINCE, UNTIL)
        self.assertEqual(rows, [])


if __name__ == "__main__":
    unittest.main()
