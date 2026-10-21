"""
Unit tests for the pipeline health rules engine.

Fixtures mirror the shape of real BindPlane Cloud responses (captured 2026-10)
with synthetic IDs and names.
"""
import unittest
from datetime import datetime, timedelta, timezone

from secops_ingestion.calculator import LogSourceMetrics
from secops_ingestion.pipeline_health import (
    IssueTracker,
    Issue,
    Thresholds,
    count_by_severity,
    evaluate_bindplane,
    evaluate_secops_parsing,
    evaluate_secops_silence,
    summarize_throughput,
    _parse_ts,
)

AGENT_A = "0196d0f0-0000-4000-8000-00000000000a"
AGENT_B = "0196d0f0-0000-4000-8000-00000000000b"


def agent(agent_id, name="host", connected=True, status=1, health=0, remote_cfg=1,
          out_of_date=False, config="cfg-linux"):
    return {
        "id": agent_id, "name": name, "connected": connected, "status": status,
        "healthStatus": health, "remoteConfigStatus": remote_cfg, "outOfDate": out_of_date,
        "version": "v1.109.0", "labels": {"configuration": config},
        "configurationStatus": {"assigned": config, "current": f"{config}:3"},
    }


def row(agent_id, node, value):
    return {"name": "otelcol_processor_throughputmeasurement_log_data_size",
            "nodeID": node, "value": value, "unit": "B/s", "agentID": agent_id}


def pipeline(agent_id, sources, dest):
    rows = []
    for i, v in enumerate(sources, 1):
        rows.append(row(agent_id, f"source/source{i}_ULID{i}", v))
        rows.append(row(agent_id, f"source/source{i}_ULID{i}/processors", v * 10))  # must be ignored
    rows.append(row(agent_id, "destination/secops-0", dest))
    rows.append(row(agent_id, "destination/secops-0/processors", dest * 10))
    return rows


def categories(issues):
    return sorted(i.category for i in issues)


class TestSummarizeThroughput(unittest.TestCase):

    def test_ignores_processor_nodes_and_sums_sources(self):
        tp = summarize_throughput(pipeline(AGENT_A, [100.0, 50.0], 160.0))
        self.assertAlmostEqual(tp[AGENT_A]["source_bps"], 150.0)
        self.assertAlmostEqual(tp[AGENT_A]["dest_bps"], 160.0)
        self.assertEqual(len(tp[AGENT_A]["sources"]), 2)

    def test_ignores_non_log_metrics_and_missing_agent(self):
        rows = [
            {"name": "otelcol_something_else", "nodeID": "source/x", "value": 5, "agentID": AGENT_A},
            row("", "source/x", 5),
        ]
        self.assertEqual(summarize_throughput(rows), {})


class TestEvaluateBindPlane(unittest.TestCase):

    def test_healthy_agent_has_no_issues(self):
        issues, rows = evaluate_bindplane([agent(AGENT_A)], [], pipeline(AGENT_A, [1000.0], 1030.0))
        self.assertEqual(issues, [])
        self.assertEqual(rows[0]["health"], "ok")
        self.assertEqual(rows[0]["loss_pct"], 0.0)  # dest > src clamps to 0

    def test_disconnected_agent_is_critical_and_not_also_silent(self):
        issues, rows = evaluate_bindplane([agent(AGENT_A, connected=False, status=0)], [], [])
        self.assertEqual(categories(issues), ["AGENT_DISCONNECTED"])
        self.assertEqual(issues[0].severity, "critical")
        self.assertFalse(rows[0]["connected"])

    def test_connected_agent_with_zero_throughput_is_silent(self):
        # Connected collector where every node reports 0 B/s.
        issues, _ = evaluate_bindplane([agent(AGENT_A)], [], pipeline(AGENT_A, [0.0, 0.0, 0.0], 0.0))
        self.assertEqual(categories(issues), ["AGENT_SILENT"])
        self.assertEqual(issues[0].evidence["reason"], "zero destination throughput")

    def test_connected_agent_with_no_metric_rows_is_silent(self):
        issues, _ = evaluate_bindplane([agent(AGENT_A)], [], [])
        self.assertEqual(categories(issues), ["AGENT_SILENT"])
        self.assertEqual(issues[0].evidence["reason"], "no throughput reported")

    def test_single_silent_source_on_live_agent_is_warning(self):
        issues, _ = evaluate_bindplane([agent(AGENT_A)], [], pipeline(AGENT_A, [500.0, 0.0], 510.0))
        self.assertEqual(categories(issues), ["SOURCE_SILENT"])
        self.assertEqual(issues[0].severity, "warning")
        self.assertIn("source2", issues[0].evidence["node_id"])

    def test_drop_warning_and_critical_thresholds(self):
        warn, _ = evaluate_bindplane([agent(AGENT_A)], [], pipeline(AGENT_A, [1000.0], 700.0))
        self.assertEqual(categories(warn), ["PIPELINE_DROP"])
        self.assertEqual(warn[0].severity, "warning")
        self.assertEqual(warn[0].evidence["loss_pct"], 30.0)

        crit, _ = evaluate_bindplane([agent(AGENT_A)], [], pipeline(AGENT_A, [1000.0], 400.0))
        self.assertEqual(crit[0].severity, "critical")

    def test_drop_ignored_below_minimum_input_rate(self):
        issues, _ = evaluate_bindplane([agent(AGENT_A)], [], pipeline(AGENT_A, [50.0], 10.0))
        self.assertEqual(issues, [])

    def test_custom_thresholds_are_respected(self):
        t = Thresholds(drop_warn_pct=5.0, drop_crit_pct=10.0)
        issues, _ = evaluate_bindplane([agent(AGENT_A)], [], pipeline(AGENT_A, [1000.0], 920.0), t)
        self.assertEqual(issues[0].severity, "warning")

    def test_remote_config_failed_and_unhealthy_and_abnormal_status(self):
        a = agent(AGENT_A, status=4, health=2, remote_cfg=3)
        issues, rows = evaluate_bindplane([a], [], pipeline(AGENT_A, [1000.0], 1000.0))
        self.assertEqual(categories(issues),
                         ["AGENT_CONFIG_FAILED", "AGENT_STATUS_ABNORMAL", "AGENT_UNHEALTHY"])
        self.assertEqual(rows[0]["health"], "critical")

    def test_rollout_errors_raise_config_issue(self):
        cfg = {"metadata": {"name": "cfg-linux"},
               "status": {"rollout": {"status": 3, "progress": {"errors": 2}}}}
        ok_cfg = {"metadata": {"name": "cfg-ok"},
                  "status": {"rollout": {"status": 4, "progress": {"errors": 0}}}}
        issues, _ = evaluate_bindplane([], [cfg, ok_cfg], [])
        self.assertEqual(categories(issues), ["CONFIG_ROLLOUT_ERROR"])
        self.assertEqual(issues[0].entity, "cfg-linux")

    def test_out_of_date_is_single_aggregate_info_issue(self):
        agents = [agent(AGENT_A, name="a", out_of_date=True), agent(AGENT_B, name="b", out_of_date=True)]
        metrics = pipeline(AGENT_A, [500.0], 500.0) + pipeline(AGENT_B, [500.0], 500.0)
        issues, _ = evaluate_bindplane(agents, [], metrics)
        self.assertEqual(categories(issues), ["AGENTS_OUT_OF_DATE"])
        self.assertEqual(issues[0].severity, "info")
        self.assertEqual(issues[0].evidence["agents"], ["a", "b"])

    def test_issues_sorted_critical_first_and_rows_worst_first(self):
        agents = [agent(AGENT_A, name="good"), agent(AGENT_B, name="bad", connected=False, status=0)]
        issues, rows = evaluate_bindplane(agents, [], pipeline(AGENT_A, [1000.0], 600.0))
        self.assertEqual([i.severity for i in issues], ["critical", "warning"])
        self.assertEqual(rows[0]["name"], "bad")


class TestSecOpsRules(unittest.TestCase):

    NOW = datetime(2026, 10, 19, 14, 0, tzinfo=timezone.utc)

    def series(self, log_type, points):
        return {"resource": {"labels": {"log_type": log_type, "collector_id": "c1"}},
                "points": [{"interval": {"endTime": end}, "value": {"int64Value": str(v)}}
                           for end, v in points]}

    def ts(self, minutes_ago):
        return (self.NOW - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")

    def every(self, log_type, start_ago, step, count, value=100):
        """`count` non-zero points every `step` minutes, newest at `start_ago`."""
        return self.series(log_type, [(self.ts(start_ago + i * step), value) for i in range(count)])

    def test_continuous_feed_flagged_after_floor(self):
        data = [
            self.every("PAN_FIREWALL", 30, 30, 10),   # healthy, last point 30 min ago
            self.every("WINEVTLOG", 180, 30, 10),     # was every 30 min, stopped 3h ago
        ]
        issues = evaluate_secops_silence(data, self.NOW)
        self.assertEqual([i.entity for i in issues], ["WINEVTLOG"])
        self.assertEqual(issues[0].evidence["silent_minutes"], 180)
        self.assertEqual(issues[0].evidence["typical_gap_minutes"], 30)
        self.assertEqual(issues[0].evidence["threshold_minutes"], 90)

    def test_batch_feed_not_flagged_within_its_cadence(self):
        # Periodic context feed: snapshot every 4h, last one 6h ago.
        data = [self.every("GCP_IAM_CONTEXT", 360, 240, 5)]
        self.assertEqual(evaluate_secops_silence(data, self.NOW), [])

    def test_batch_feed_flagged_when_it_misses_several_cycles(self):
        data = [self.every("GCP_IAM_CONTEXT", 800, 240, 4)]
        issues = evaluate_secops_silence(data, self.NOW)
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0].evidence["threshold_minutes"], 720)

    def test_insufficient_history_is_skipped(self):
        data = [self.every("DAILY_FEED", 1000, 60, 3)]
        self.assertEqual(evaluate_secops_silence(data, self.NOW), [])

    def test_zero_valued_points_do_not_count_as_arrivals(self):
        data = [
            self.every("AUDITD", 180, 30, 6),
            self.series("AUDITD", [(self.ts(30), 0), (self.ts(60), 0)]),
        ]
        self.assertEqual([i.entity for i in evaluate_secops_silence(data, self.NOW)], ["AUDITD"])

    def test_log_type_never_seen_is_not_flagged(self):
        data = [self.series("EMPTY", [(self.ts(30), 0)])]
        self.assertEqual(evaluate_secops_silence(data, self.NOW), [])

    def test_threshold_and_ignore_overrides(self):
        data = [self.every("AUDITD", 45, 10, 10)]
        self.assertEqual(evaluate_secops_silence(data, self.NOW), [])
        tight = Thresholds(secops_silent_after_minutes=30)
        self.assertEqual(len(evaluate_secops_silence(data, self.NOW, tight)), 1)
        ignored = Thresholds(secops_silent_after_minutes=30, secops_silent_ignore=("auditd",))
        self.assertEqual(evaluate_secops_silence(data, self.NOW, ignored), [])

    def test_parser_rules(self):
        failing = LogSourceMetrics(log_type="BAD", record_count=100, normalized_event_count=0)
        low = LogSourceMetrics(log_type="LOW", record_count=100, normalized_event_count=50)
        fine = LogSourceMetrics(log_type="OK", record_count=100, normalized_event_count=99)
        for s in (failing, low, fine):
            s.calculate_derived_fields()
        issues = evaluate_secops_parsing([failing, low, fine])
        self.assertEqual({(i.entity, i.severity) for i in issues},
                         {("BAD", "critical"), ("LOW", "warning")})


class TestIssueTracker(unittest.TestCase):

    def make(self, entity="x", category="AGENT_SILENT"):
        return Issue("critical", category, "agent", entity, "msg")

    def test_first_seen_persists_and_resolution_tracked(self):
        tracker = IssueTracker(resolved_retention_seconds=3600)
        t0 = datetime(2026, 10, 19, 14, 0, tzinfo=timezone.utc)
        t1 = t0 + timedelta(minutes=5)
        t2 = t0 + timedelta(minutes=10)

        active, resolved = tracker.update([self.make()], t0)
        self.assertEqual(active[0].first_seen, "2026-10-19T14:00:00Z")

        active, _ = tracker.update([self.make()], t1)
        self.assertEqual(active[0].first_seen, "2026-10-19T14:00:00Z")
        self.assertEqual(active[0].last_seen, "2026-10-19T14:05:00Z")

        active, resolved = tracker.update([], t2)
        self.assertEqual(active, [])
        self.assertEqual([r.entity for r in resolved], ["x"])

    def test_resolved_expires_and_reoccurrence_resets_first_seen(self):
        tracker = IssueTracker(resolved_retention_seconds=60)
        t0 = datetime(2026, 10, 19, 14, 0, tzinfo=timezone.utc)
        tracker.update([self.make()], t0)
        tracker.update([], t0 + timedelta(seconds=30))
        _, resolved = tracker.update([], t0 + timedelta(seconds=200))
        self.assertEqual(resolved, [])

        active, _ = tracker.update([self.make()], t0 + timedelta(seconds=300))
        self.assertEqual(active[0].first_seen, "2026-10-19T14:05:00Z")


class TestHelpers(unittest.TestCase):

    def test_count_by_severity(self):
        issues = [Issue("critical", "a", "agent", "1", ""), Issue("info", "b", "agent", "2", "")]
        self.assertEqual(count_by_severity(issues), {"critical": 1, "warning": 0, "info": 1})

    def test_parse_ts_variants(self):
        self.assertEqual(_parse_ts("2026-10-08T15:17:25.368993946Z").microsecond, 368993)
        self.assertEqual(_parse_ts("2026-10-19T14:00:00Z").tzinfo, timezone.utc)
        self.assertIsNone(_parse_ts("not-a-date"))
        self.assertIsNone(_parse_ts(""))


if __name__ == "__main__":
    unittest.main()
