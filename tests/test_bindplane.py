"""
Tests for the BindPlane client and the /api/pipeline/health endpoint.
"""
import os
import tempfile
import unittest
from unittest import mock

import requests

from secops_ingestion.bindplane import (
    BindPlaneClient,
    BindPlaneError,
    MockBindPlaneClient,
)

ACCOUNT = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
SECRET = "test-key.super-secret-value"
AGENT = "0196d0f0-9c1a-7f41-9e95-2d4487a65701"


def fake_response(status=200, payload=None, json_error=False):
    resp = mock.Mock()
    resp.status_code = status
    if json_error:
        resp.json.side_effect = ValueError("not json")
    else:
        resp.json.return_value = payload if payload is not None else {}
    return resp


class TestBindPlaneClient(unittest.TestCase):

    def setUp(self):
        self.session = mock.Mock(spec=requests.Session)
        self.client = BindPlaneClient(SECRET, ACCOUNT, session=self.session)

    def test_sends_both_auth_headers(self):
        self.session.request.return_value = fake_response(payload={"agents": [{"id": AGENT}]})
        self.assertEqual(self.client.list_agents(), [{"id": AGENT}])
        _, kwargs = self.session.request.call_args
        self.assertEqual(kwargs["headers"]["X-Bindplane-Api-Key"], SECRET)
        self.assertEqual(kwargs["headers"]["X-Bindplane-Account-ID"], ACCOUNT)
        self.assertEqual(kwargs["timeout"], 15)

    def test_throughput_uses_graphql_variables_not_interpolation(self):
        self.session.request.return_value = fake_response(
            payload={"data": {"agentMetrics": {"metrics": [{"agentID": AGENT}]}}}
        )
        rows = self.client.agent_throughput([AGENT, "bad\"}) { injected }"], period="1h")
        self.assertEqual(rows, [{"agentID": AGENT}])
        method, url = self.session.request.call_args[0]
        body = self.session.request.call_args[1]["json"]
        self.assertEqual((method, url), ("POST", "https://app.bindplane.com/v1/graphql"))
        self.assertEqual(body["variables"], {"period": "1h", "ids": [AGENT]})
        self.assertNotIn(AGENT, body["query"])

    def test_throughput_skips_call_when_no_valid_ids(self):
        self.assertEqual(self.client.agent_throughput(["nope"]), [])
        self.session.request.assert_not_called()

    def test_throughput_rejects_unknown_period(self):
        with self.assertRaises(ValueError):
            self.client.agent_throughput([AGENT], period="7d")

    def test_graphql_errors_raise(self):
        self.session.request.return_value = fake_response(
            payload={"errors": [{"message": "internal system error"}], "data": None}
        )
        with self.assertRaisesRegex(BindPlaneError, "internal system error"):
            self.client.agent_throughput([AGENT])

    def test_auth_failure_message_never_contains_key(self):
        self.session.request.return_value = fake_response(status=401)
        with self.assertRaises(BindPlaneError) as ctx:
            self.client.list_agents()
        self.assertIn("HTTP 401", str(ctx.exception))
        self.assertNotIn(SECRET, str(ctx.exception))

    def test_network_error_message_never_contains_key(self):
        self.session.request.side_effect = requests.ConnectionError(f"boom {SECRET}")
        with self.assertRaises(BindPlaneError) as ctx:
            self.client.list_configurations()
        self.assertNotIn(SECRET, str(ctx.exception))
        self.assertIsNone(ctx.exception.__cause__)

    def test_server_error_and_non_json(self):
        self.session.request.return_value = fake_response(status=502)
        with self.assertRaisesRegex(BindPlaneError, "HTTP 502"):
            self.client.list_agents()
        self.session.request.return_value = fake_response(json_error=True)
        with self.assertRaisesRegex(BindPlaneError, "non-JSON"):
            self.client.list_agents()

    def test_repr_hides_key(self):
        self.assertNotIn(SECRET, repr(self.client))

    def test_constructor_validation(self):
        with self.assertRaises(ValueError):
            BindPlaneClient("", ACCOUNT)
        with self.assertRaises(ValueError):
            BindPlaneClient(SECRET, "not-a-ulid")
        with self.assertRaises(ValueError):
            BindPlaneClient(SECRET, ACCOUNT, base_url="http://insecure.example")


class TestFromEnv(unittest.TestCase):

    def test_reads_key_from_file(self):
        with tempfile.NamedTemporaryFile("w", delete=False) as fh:
            fh.write(SECRET + "\n")
            path = fh.name
        try:
            env = {"BINDPLANE_API_KEY_FILE": path, "BINDPLANE_ACCOUNT_ID": ACCOUNT}
            with mock.patch.dict(os.environ, env, clear=True):
                client = BindPlaneClient.from_env()
            self.assertIsNotNone(client)
            self.assertEqual(client._api_key, SECRET)
        finally:
            os.unlink(path)

    def test_returns_none_when_unconfigured_or_file_missing(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(BindPlaneClient.from_env())
        env = {"BINDPLANE_API_KEY_FILE": "/nonexistent/key", "BINDPLANE_ACCOUNT_ID": ACCOUNT}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertIsNone(BindPlaneClient.from_env())


class TestPipelineHealthEndpoint(unittest.TestCase):

    def setUp(self):
        import app as app_module
        self.mod = app_module
        self.mod.app.config["MOCK_MODE"] = True
        self.mod._pipeline_cache.clear()
        self.mod._issue_trackers.clear()
        self.client = self.mod.app.test_client()

    def test_mock_mode_surfaces_every_bindplane_rule(self):
        res = self.client.get("/api/pipeline/health")
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        cats = {i["category"] for i in data["issues"]}
        for expected in ("AGENT_DISCONNECTED", "AGENT_SILENT", "SOURCE_SILENT",
                         "PIPELINE_DROP", "CONFIG_ROLLOUT_ERROR", "AGENT_CONFIG_FAILED",
                         "AGENTS_OUT_OF_DATE"):
            self.assertIn(expected, cats)
        self.assertTrue(data["bindplane"]["configured"])
        self.assertTrue(data["bindplane"]["mock"])
        self.assertEqual(data["counts"]["critical"],
                         sum(1 for i in data["issues"] if i["severity"] == "critical"))
        self.assertEqual(data["issues"][0]["severity"], "critical")
        self.assertTrue(all(i["first_seen"] for i in data["issues"]))
        self.assertEqual(len(data["agents"]), len(MockBindPlaneClient._AGENTS))

    def test_invalid_window_falls_back_to_1h(self):
        data = self.client.get("/api/pipeline/health?window=evil").get_json()
        self.assertEqual(data["window"], "1h")

    def test_response_is_cached(self):
        with mock.patch.object(self.mod, "build_pipeline_health",
                               wraps=self.mod.build_pipeline_health) as spy:
            self.client.get("/api/pipeline/health")
            self.client.get("/api/pipeline/health")
            self.assertEqual(spy.call_count, 1)

    def test_unconfigured_bindplane_reports_message_not_error(self):
        self.mod.app.config["MOCK_MODE"] = False
        try:
            with mock.patch.object(self.mod, "get_bindplane_client", return_value=None), \
                 mock.patch.object(self.mod, "fetch_period_raw", return_value={}), \
                 mock.patch.object(self.mod, "fetch_period_summary",
                                   return_value=mock.Mock(log_types=[], end_time="2026-10-19T18:00:00Z")):
                data = self.client.get("/api/pipeline/health").get_json()
        finally:
            self.mod.app.config["MOCK_MODE"] = True
        self.assertFalse(data["bindplane"]["configured"])
        self.assertIn("BINDPLANE_ACCOUNT_ID", data["bindplane"]["message"])
        self.assertEqual(data["issues"], [])

    def test_delivery_section_present_in_mock_mode(self):
        data = self.client.get("/api/pipeline/health").get_json()
        self.assertIn("delivery", data)
        self.assertTrue(data["delivery_info"]["available"])
        self.assertIn("aaaa1111-aaaa-1111-aaaa-1111aaaa1112", data["delivery_info"]["secops_collector_ids"])
        self.assertEqual(len(data["delivery_info"]["recent_window"]), 2)

    def test_collector_ids_overridable(self):
        with mock.patch.dict(os.environ, {"BINDPLANE_SECOPS_COLLECTOR_IDS": "x-1, x-2"}):
            data = self.client.get("/api/pipeline/health").get_json()
        self.assertEqual(data["delivery_info"]["secops_collector_ids"], ["x-1", "x-2"])

    def test_upstream_failures_become_issues_not_5xx(self):
        from secops_ingestion.client import MonitoringAuthError
        broken = mock.Mock()
        broken.list_agents.side_effect = BindPlaneError("BindPlane returned HTTP 503 for /v1/agents")
        with mock.patch.object(self.mod, "get_bindplane_client", return_value=broken), \
             mock.patch.object(self.mod, "fetch_period_raw", side_effect=MonitoringAuthError("x")):
            res = self.client.get("/api/pipeline/health")
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertEqual({i["category"] for i in data["issues"]},
                         {"BINDPLANE_API_UNAVAILABLE", "SECOPS_TELEMETRY_UNAVAILABLE"})
        self.assertFalse(data["secops"]["available"])


if __name__ == "__main__":
    unittest.main()
