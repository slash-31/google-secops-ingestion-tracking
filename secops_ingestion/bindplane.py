"""
BindPlane Cloud API client.

Author: Joshua Koch
Created: 2026-10-19
Last Updated: 2026-10-19
Version: 1.0.0
Description: Read-only client for BindPlane Cloud (app.bindplane.com). Pulls
             agent state, configuration rollout status, and per-agent pipeline
             throughput so collector outages and drops can be surfaced on the
             dashboard alongside SecOps ingestion telemetry.

API notes (verified against BindPlane Cloud, 2026-10):
  * Every request needs BOTH `X-Bindplane-Api-Key` and `X-Bindplane-Account-ID`
    (the BindPlane *project* ID). API keys cannot discover their own account.
  * Throughput is only exposed through GraphQL `agentMetrics(period, ids)`.
    `ids` is mandatory in practice -- omitting it returns "internal system error".
  * Throughput is bytes/sec (`log_data_size`), not record counts.
"""
import os
import re
import logging
from typing import Any, Dict, List, Optional

import requests

logger = logging.getLogger("secops_ingestion.bindplane")

DEFAULT_BASE_URL = "https://app.bindplane.com"

# Periods accepted by BindPlane's metrics API; kept as an allowlist so a query
# parameter can never steer arbitrary strings into the upstream request.
ALLOWED_PERIODS = ("5m", "1h", "24h")

# BindPlane agent IDs are UUIDs; account IDs are 26-char ULIDs.
_AGENT_ID_RE = re.compile(r"^[0-9a-fA-F-]{36}$")
_ACCOUNT_ID_RE = re.compile(r"^[0-9A-Z]{26}$")

# Variables are passed separately rather than interpolated into the query text.
_AGENT_METRICS_QUERY = (
    "query AgentMetrics($period: String!, $ids: [ID!]) { "
    "agentMetrics(period: $period, ids: $ids) { "
    "metrics { name nodeID value unit agentID } } }"
)


class BindPlaneError(Exception):
    """Raised when the BindPlane API cannot be queried. Never carries the API key."""


def _read_secret_file(path: str) -> str:
    """Read a single-line secret from disk, stripping whitespace."""
    with open(os.path.expanduser(path), "r", encoding="utf-8") as fh:
        return fh.read().strip()


class BindPlaneClient:
    """Read-only BindPlane Cloud API client."""

    def __init__(
        self,
        api_key: str,
        account_id: str,
        base_url: str = DEFAULT_BASE_URL,
        timeout: int = 15,
        session: Optional[requests.Session] = None,
    ):
        if not api_key:
            raise ValueError("BindPlane API key is required")
        if not _ACCOUNT_ID_RE.match(account_id or ""):
            raise ValueError("BindPlane account ID must be a 26-character ULID")
        if not base_url.startswith("https://"):
            raise ValueError("BindPlane base URL must use https")

        self._api_key = api_key
        self.account_id = account_id
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._session = session or requests.Session()

    def __repr__(self) -> str:
        # Explicit repr so the key never leaks via logging or debugger output.
        return f"BindPlaneClient(base_url={self.base_url!r}, account_id={self.account_id!r})"

    @classmethod
    def from_env(cls) -> Optional["BindPlaneClient"]:
        """
        Build a client from environment configuration, or return None if unset.

        BINDPLANE_API_KEY       raw key (prefer the file variant)
        BINDPLANE_API_KEY_FILE  path to a file containing the key
        BINDPLANE_ACCOUNT_ID    BindPlane project/account ID
        BINDPLANE_BASE_URL      optional, defaults to BindPlane Cloud
        """
        account_id = os.environ.get("BINDPLANE_ACCOUNT_ID", "").strip()
        api_key = os.environ.get("BINDPLANE_API_KEY", "").strip()
        key_file = os.environ.get("BINDPLANE_API_KEY_FILE", "").strip()

        if not api_key and key_file:
            try:
                api_key = _read_secret_file(key_file)
            except OSError as exc:
                logger.error("Cannot read BINDPLANE_API_KEY_FILE: %s", exc.strerror)
                return None

        if not api_key or not account_id:
            return None

        return cls(
            api_key=api_key,
            account_id=account_id,
            base_url=os.environ.get("BINDPLANE_BASE_URL", DEFAULT_BASE_URL),
        )

    def _headers(self) -> Dict[str, str]:
        return {
            "X-Bindplane-Api-Key": self._api_key,
            "X-Bindplane-Account-ID": self.account_id,
            "Accept": "application/json",
            "User-Agent": "SecOps-Ingestion-Dashboard/1.0",
        }

    def _request(self, method: str, path: str, json_body: Optional[Dict] = None) -> Dict[str, Any]:
        url = f"{self.base_url}{path}"
        try:
            resp = self._session.request(
                method, url, headers=self._headers(), json=json_body, timeout=self.timeout
            )
        except requests.RequestException as exc:
            # Exception text from requests can include headers on some adapters;
            # report only the exception class.
            raise BindPlaneError(f"BindPlane request to {path} failed: {type(exc).__name__}") from None

        if resp.status_code in (401, 403):
            raise BindPlaneError(
                f"BindPlane rejected credentials for {path} (HTTP {resp.status_code}). "
                "Check BINDPLANE_API_KEY and BINDPLANE_ACCOUNT_ID."
            )
        if resp.status_code >= 400:
            raise BindPlaneError(f"BindPlane returned HTTP {resp.status_code} for {path}")

        try:
            return resp.json()
        except ValueError:
            raise BindPlaneError(f"BindPlane returned non-JSON content for {path}") from None

    def list_agents(self) -> List[Dict[str, Any]]:
        """Return all agents in the account."""
        return self._request("GET", "/v1/agents").get("agents", []) or []

    def list_configurations(self) -> List[Dict[str, Any]]:
        """Return all configurations, including rollout status."""
        return self._request("GET", "/v1/configurations").get("configurations", []) or []

    def agent_throughput(self, agent_ids: List[str], period: str = "1h") -> List[Dict[str, Any]]:
        """
        Return throughput metric rows for the given agents over `period`.

        Each row: {name, nodeID, value (B/s), unit, agentID}.
        """
        if period not in ALLOWED_PERIODS:
            raise ValueError(f"period must be one of {ALLOWED_PERIODS}")
        ids = [a for a in agent_ids if _AGENT_ID_RE.match(a or "")]
        if not ids:
            return []

        payload = {"query": _AGENT_METRICS_QUERY, "variables": {"period": period, "ids": ids}}
        data = self._request("POST", "/v1/graphql", json_body=payload)
        if data.get("errors"):
            messages = "; ".join(str(e.get("message", "unknown")) for e in data["errors"])
            raise BindPlaneError(f"BindPlane GraphQL error: {messages}")
        return ((data.get("data") or {}).get("agentMetrics") or {}).get("metrics", []) or []


class MockBindPlaneClient:
    """
    Synthetic BindPlane data for offline/demo mode.

    Deliberately includes one disconnected agent, one connected-but-silent
    agent, one lossy pipeline, and one failed rollout so every rule renders.
    """

    account_id = "MOCK0000000000000000000000"

    _AGENTS = [
        ("11111111-1111-4111-8111-111111111111", "win-dc01", "edge-windows-ad", True, 1, 1),
        ("22222222-2222-4222-8222-222222222222", "syslog-01", "edge-syslog", True, 1, 1),
        ("33333333-3333-4333-8333-333333333333", "linux-web01", "edge-linux", True, 1, 1),
        ("44444444-4444-4444-8444-444444444444", "linux-web02", "edge-linux", True, 1, 1),
        ("55555555-5555-4555-8555-555555555555", "meraki-01", "edge-meraki", False, 0, 1),
        ("66666666-6666-4666-8666-666666666666", "win-ws17", "edge-windows-hosts", True, 1, 3),
    ]

    # agent_id -> (source B/s per source node, destination B/s)
    _THROUGHPUT = {
        "11111111-1111-4111-8111-111111111111": ([700.0, 12.0], 715.0),
        "22222222-2222-4222-8222-222222222222": ([380000.0, 4200.0], 220000.0),  # lossy
        "33333333-3333-4333-8333-333333333333": ([2100.0, 0.0, 300.0], 2450.0),  # one source silent
        "44444444-4444-4444-8444-444444444444": ([0.0, 0.0, 0.0], 0.0),          # silent agent
        "66666666-6666-4666-8666-666666666666": ([90.0], 91.0),
    }

    def list_agents(self) -> List[Dict[str, Any]]:
        agents = []
        for agent_id, name, cfg, connected, status, remote_cfg in self._AGENTS:
            agents.append({
                "id": agent_id,
                "name": name,
                "hostname": name,
                "connected": connected,
                "status": status,
                "healthStatus": 0,
                "remoteConfigStatus": remote_cfg,
                "connectedAt": "2026-10-19T14:00:00Z" if connected else None,
                "disconnectedAt": None if connected else "2026-10-19T12:10:00Z",
                "outOfDate": agent_id.startswith("1"),
                "version": "v1.109.0",
                "platform": "windows" if name.startswith("win") else "linux",
                "configurationStatus": {"assigned": cfg, "current": f"{cfg}:3"},
                "labels": {"configuration": cfg},
            })
        return agents

    def list_configurations(self) -> List[Dict[str, Any]]:
        names = sorted({a[2] for a in self._AGENTS})
        configs = []
        for name in names:
            errors = 1 if name == "edge-windows-hosts" else 0
            configs.append({
                "metadata": {"name": name},
                "spec": {"sources": [], "destinations": [{"name": "secops-mock:1"}]},
                "status": {"rollout": {"name": name, "status": 3 if errors else 4,
                                       "progress": {"completed": 1, "errors": errors,
                                                    "pending": 0, "waiting": 0}}},
            })
        return configs

    def agent_throughput(self, agent_ids: List[str], period: str = "1h") -> List[Dict[str, Any]]:
        if period not in ALLOWED_PERIODS:
            raise ValueError(f"period must be one of {ALLOWED_PERIODS}")
        rows = []
        for agent_id in agent_ids:
            if agent_id not in self._THROUGHPUT:
                continue
            sources, dest = self._THROUGHPUT[agent_id]
            for i, val in enumerate(sources, start=1):
                rows.append(self._row(agent_id, f"source/source{i}_MOCK{i}", val))
                rows.append(self._row(agent_id, f"source/source{i}_MOCK{i}/processors", val))
            rows.append(self._row(agent_id, "destination/secops-mock-0", dest))
            rows.append(self._row(agent_id, "destination/secops-mock-0/processors", dest))
        return rows

    @staticmethod
    def _row(agent_id: str, node_id: str, value: float) -> Dict[str, Any]:
        return {
            "name": "otelcol_processor_throughputmeasurement_log_data_size",
            "nodeID": node_id,
            "value": value,
            "unit": "B/s",
            "agentID": agent_id,
        }
