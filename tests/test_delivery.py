"""
Tests for BindPlane -> SecOps delivery reconciliation.

Fixture shapes mirror real BindPlane configurations/agentMetrics and Cloud
Monitoring timeSeries (captured 2026-10) with synthetic IDs.
"""
import unittest
from datetime import datetime, timedelta, timezone

from secops_ingestion.delivery import (
    GroupTotals,
    bindplane_bytes_by_group,
    detect_idle_sources,
    evaluate_delivery,
    fleet_baseline_ratio,
    map_sources,
    parse_source_node,
    recent_window,
    secops_bytes_by_log_type,
)
from secops_ingestion.pipeline_health import Thresholds

BP_COLLECTOR = "aaaa1111-aaaa-1111-aaaa-1111aaaa1112"
AGENT_1 = "0196d0f1-0000-4000-8000-000000000001"
AGENT_2 = "0196d0f1-0000-4000-8000-000000000002"


def standardization(*log_types):
    return {
        "type": "google_secops_standardization:1",
        "parameters": [
            {"name": "telemetry_types", "value": ["Logs"]},
            {"name": "secops_field", "value": [{"logType": lt, "namespace": ""} for lt in log_types]},
        ],
    }


def config(name, sources):
    return {"metadata": {"name": name}, "spec": {"sources": sources}}


CONFIGS = [
    config("linux", [
        {"id": "SRCAUDIT", "type": "file_v2:1", "processors": [standardization("AUDITD")]},
        {"id": "SRCSYSMON", "type": "file_v2:1",
         "processors": [{"type": "filter-by-condition:1", "parameters": []}, standardization("LINUX_SYSMON")]},
        {"id": "SRCNOLT", "type": "file_v2:1", "processors": []},  # no SecOps log type -> ignored
    ]),
    config("windows", [
        {"id": "SRCWIN", "type": "windowsevents_v3:1",
         "processors": [standardization("WINEVTLOG", "WINDOWS_SYSMON", "WINEVTLOG")]},
    ]),
    config("okta", [
        {"id": "SRCOKTA", "name": "Okta_Ingestion:3", "processors": [standardization("OKTA")]},
    ]),
]


def metric(agent_id, node, bps):
    return {"name": "otelcol_processor_throughputmeasurement_log_data_size",
            "nodeID": node, "value": bps, "unit": "B/s", "agentID": agent_id}


def totals(nbytes, agent="a1"):
    g = GroupTotals(bytes=nbytes)
    g.bytes_by_agent[agent] = nbytes
    return g


class TestSourceMapping(unittest.TestCase):

    # Real BindPlane source ids are ULIDs (no underscores); node ids are
    # 'source/<name>_<id>', so the id is the segment after the last underscore.
    def test_map_sources_extracts_log_types_and_filters(self):
        sources = map_sources(CONFIGS)
        self.assertEqual(set(sources), {"SRCAUDIT", "SRCSYSMON", "SRCWIN", "SRCOKTA"})
        self.assertEqual(sources["SRCAUDIT"].log_types, ("AUDITD",))
        self.assertFalse(sources["SRCAUDIT"].filters)
        self.assertTrue(sources["SRCSYSMON"].filters)
        # Multi-log-type source collapses to a sorted, de-duplicated group.
        self.assertEqual(sources["SRCWIN"].log_types, ("WINDOWS_SYSMON", "WINEVTLOG"))
        self.assertEqual(sources["SRCOKTA"].source_type, "Okta_Ingestion:3")

    def test_parse_source_node_variants(self):
        self.assertEqual(parse_source_node("source/source2_SRC_X"), ("X", False))
        self.assertEqual(parse_source_node("source/source0_01M4C5PYVR33/processors"), ("01M4C5PYVR33", True))
        self.assertEqual(parse_source_node("source/Okta_Ingestion_01M4E1F9"), ("01M4E1F9", False))
        self.assertEqual(parse_source_node("destination/secops-0"), (None, False))
        self.assertEqual(parse_source_node("source/noid"), (None, False))


class TestBindPlaneBytes(unittest.TestCase):

    def test_prefers_processor_node_and_converts_to_bytes(self):
        sources = map_sources([config("c", [
            {"id": "S1", "processors": [standardization("AUDITD")]},
            {"id": "S2", "processors": [standardization("WAZUH")]},
        ])])
        rows = [
            metric(AGENT_1, "source/source0_S1", 1000.0),
            metric(AGENT_1, "source/source0_S1/processors", 400.0),  # post-filter wins
            metric(AGENT_2, "source/source0_S1", 100.0),              # raw-only fallback
            metric(AGENT_1, "source/source1_S2", 50.0),
            metric(AGENT_1, "source/source9_UNKNOWN", 9999.0),        # unmapped source ignored
            metric(AGENT_1, "destination/secops-0", 9999.0),
        ]
        groups = bindplane_bytes_by_group(rows, sources, 3600, {AGENT_1: "host-1"})
        self.assertAlmostEqual(groups[("AUDITD",)].bytes, (400.0 + 100.0) * 3600)
        self.assertAlmostEqual(groups[("WAZUH",)].bytes, 50.0 * 3600)
        top = groups[("AUDITD",)].top_agents()
        self.assertEqual(top[0]["agent"], "host-1")
        self.assertEqual(top[0]["share_pct"], 80.0)
        self.assertEqual(top[1]["agent"], AGENT_2)  # falls back to id without a name


class TestSecOpsBytes(unittest.TestCase):

    END = datetime(2026, 10, 19, 18, 0, tzinfo=timezone.utc)

    def series(self, log_type, collector, points):
        return {"resource": {"labels": {"log_type": log_type, "collector_id": collector}},
                "points": [{"interval": {"endTime": (self.END - timedelta(minutes=m)).strftime("%Y-%m-%dT%H:%M:%SZ")},
                            "value": {"int64Value": str(v)}} for m, v in points]}

    def test_filters_collector_and_window_bounds(self):
        data = [
            self.series("AUDITD", BP_COLLECTOR, [(0, 1), (30, 10), (60, 100), (90, 1000)]),
            self.series("AUDITD", "dddddddd-dddd-dddd-dddd-dddddddddddd", [(30, 5000)]),
        ]
        since, until = recent_window(self.END)  # (end-90m, end-30m]
        recent = secops_bytes_by_log_type(data, [BP_COLLECTOR], since, until)
        self.assertEqual(recent, {"AUDITD": 110.0})   # excludes newest (filling) and oldest buckets
        everything = secops_bytes_by_log_type(data, [BP_COLLECTOR])
        self.assertEqual(everything, {"AUDITD": 1111.0})
        baseline = secops_bytes_by_log_type(data, [BP_COLLECTOR], until=until)
        self.assertEqual(baseline, {"AUDITD": 1110.0})

    def test_recent_window_shape(self):
        since, until = recent_window(self.END, lag_minutes=30, minutes=60)
        self.assertEqual(until, self.END - timedelta(minutes=30))
        self.assertEqual(since, self.END - timedelta(minutes=90))


class TestEvaluateDelivery(unittest.TestCase):

    MB = 1_000_000.0

    def baseline_fleet(self):
        """Three healthy groups at ratio 0.4 so the fleet median is 0.4."""
        bp = {(lt,): totals(100 * self.MB) for lt in ("A", "B", "C")}
        so = {lt: 40 * self.MB for lt in ("A", "B", "C")}
        return bp, so

    def run_eval(self, recent_bp, recent_so, extra_base_bp=None, extra_base_so=None, t=None):
        base_bp, base_so = self.baseline_fleet()
        base_bp.update(extra_base_bp or {})
        base_so.update(extra_base_so or {})
        return evaluate_delivery(recent_bp, base_bp, recent_so, base_so, t)

    def test_healthy_ratio_no_issue(self):
        issues, rows = self.run_eval({("A",): totals(10 * self.MB)}, {"A": 4.2 * self.MB})
        self.assertEqual(issues, [])
        row = next(r for r in rows if r["group"] == "A")
        self.assertEqual(row["status"], "ok")
        self.assertEqual(row["ratio_baseline"], 0.4)
        self.assertEqual(row["fleet_ratio_baseline"], 0.4)

    def test_delivery_stopped(self):
        issues, rows = self.run_eval({("A",): totals(10 * self.MB)}, {})
        self.assertEqual([i.category for i in issues], ["DELIVERY_STOPPED"])
        self.assertEqual(issues[0].severity, "critical")
        self.assertEqual(rows[0]["status"], "stopped")

    def test_delivery_gap_warning_and_critical(self):
        warn, _ = self.run_eval({("A",): totals(10 * self.MB)}, {"A": 1.6 * self.MB})  # 0.16 vs 0.4 = -60%
        self.assertEqual([(i.category, i.severity) for i in warn], [("DELIVERY_GAP", "warning")])
        self.assertEqual(warn[0].evidence["ratio_change_pct"], -60.0)

        crit, _ = self.run_eval({("A",): totals(10 * self.MB)}, {"A": 0.4 * self.MB})  # -90%
        self.assertEqual(crit[0].severity, "critical")

    def test_ratio_increase_is_not_a_gap(self):
        issues, _ = self.run_eval({("A",): totals(10 * self.MB)}, {"A": 9 * self.MB})
        self.assertEqual(issues, [])

    def test_low_volume_ignored(self):
        issues, rows = self.run_eval({("A",): totals(0.1 * self.MB)}, {})
        self.assertEqual(issues, [])
        self.assertEqual(next(r for r in rows if r["group"] == "A")["status"], "low_volume")

    def test_log_type_never_in_secops(self):
        issues, _ = self.run_eval({}, {}, extra_base_bp={("TYPO_LOGTYPE",): totals(50 * self.MB)})
        self.assertEqual([i.category for i in issues], ["LOG_TYPE_NOT_IN_SECOPS"])

    def test_baseline_outlier_flags_dropped_burst_with_culprit(self):
        # Mirrors Argolis AUDITD: one agent's 24h burst never reached SecOps.
        burst = GroupTotals(bytes=1000 * self.MB, bytes_by_agent={"binaryfu-25": 930 * self.MB, "h2": 70 * self.MB})
        issues, rows = self.run_eval(
            {("AUDITD",): totals(10 * self.MB)}, {"AUDITD": 4 * self.MB},
            extra_base_bp={("AUDITD",): burst}, extra_base_so={"AUDITD": 25 * self.MB},
        )
        self.assertEqual([i.category for i in issues], ["DELIVERY_GAP_24H"])
        self.assertIn("binaryfu-25 accounts for 93.0%", issues[0].message)
        self.assertEqual(next(r for r in rows if r["group"] == "AUDITD")["status"], "gap_24h")

    def test_multi_log_type_group_sums_secops_side(self):
        group = ("WINDOWS_SYSMON", "WINEVTLOG")
        issues, rows = self.run_eval(
            {group: totals(10 * self.MB)}, {"WINEVTLOG": 3 * self.MB, "WINDOWS_SYSMON": 1 * self.MB},
            extra_base_bp={group: totals(100 * self.MB)},
            extra_base_so={"WINEVTLOG": 30 * self.MB, "WINDOWS_SYSMON": 10 * self.MB},
        )
        self.assertEqual(issues, [])
        row = next(r for r in rows if r["group"] == "WINDOWS_SYSMON + WINEVTLOG")
        self.assertEqual(row["secops_bytes_recent"], 4_000_000)

    def test_rows_sorted_worst_first(self):
        _, rows = self.run_eval({("A",): totals(10 * self.MB), ("B",): totals(10 * self.MB)},
                                {"B": 4 * self.MB})
        self.assertEqual(rows[0]["group"], "A")
        self.assertEqual(rows[0]["status"], "stopped")

    def test_custom_thresholds(self):
        t = Thresholds(delivery_gap_warn_pct=10.0, delivery_gap_crit_pct=30.0)
        issues, _ = self.run_eval({("A",): totals(10 * self.MB)}, {"A": 2.6 * self.MB}, t=t)  # -35%
        self.assertEqual(issues[0].severity, "critical")


class TestFleetRatio(unittest.TestCase):

    def test_median_and_minimum_groups(self):
        mb = 1_000_000.0
        bp = {("A",): totals(100 * mb), ("B",): totals(100 * mb), ("C",): totals(100 * mb), ("D",): totals(100 * mb)}
        so = {"A": 10 * mb, "B": 30 * mb, "C": 50 * mb, "D": 70 * mb}
        self.assertEqual(fleet_baseline_ratio(bp, so, mb), 0.4)  # even count -> mean of middle two
        self.assertIsNone(fleet_baseline_ratio({("A",): totals(100 * mb)}, {"A": 1 * mb}, mb))


class TestIdleSources(unittest.TestCase):

    def test_flags_configured_source_with_no_rows(self):
        sources = map_sources(CONFIGS)
        agents = [
            {"id": AGENT_1, "name": "lin-1", "labels": {"configuration": "linux"}},
            {"id": AGENT_2, "name": "lin-2", "labels": {"configuration": "linux"}},  # no rows at all
        ]
        rows = [metric(AGENT_1, "source/source0_SRCAUDIT", 10.0), metric(AGENT_1, "destination/secops-0", 10.0)]
        issues = detect_idle_sources(agents, sources, rows)
        self.assertEqual([i.entity for i in issues], ["lin-1 / LINUX_SYSMON"])
        self.assertEqual(issues[0].category, "SOURCE_NO_DATA_24H")


if __name__ == "__main__":
    unittest.main()
