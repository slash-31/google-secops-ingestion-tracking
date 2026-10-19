"""
Pipeline health rules engine.

Author: Joshua Koch
Created: 2026-10-19
Last Updated: 2026-10-19
Version: 1.0.0
Description: Turns BindPlane agent/config/throughput data and SecOps ingestion
             telemetry into a prioritised list of issues (outages, silent
             sources, pipeline drops, rollout failures, parser problems).

             Rule functions are pure (data in, issues out) so they can be unit
             tested against captured API fixtures. Only IssueTracker holds
             state, and only in memory.
"""
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from threading import Lock
from typing import Any, Dict, List, Optional, Tuple

SEVERITY_CRITICAL = "critical"
SEVERITY_WARNING = "warning"
SEVERITY_INFO = "info"
_SEVERITY_RANK = {SEVERITY_CRITICAL: 0, SEVERITY_WARNING: 1, SEVERITY_INFO: 2}

# OpAMP RemoteConfigStatuses enum (protocol-defined, not BindPlane-specific).
OPAMP_REMOTE_CONFIG_FAILED = 3

# BindPlane agent `status`: only these two values are relied on. Anything else
# is surfaced verbatim rather than guessed at.
AGENT_STATUS_DISCONNECTED = 0
AGENT_STATUS_CONNECTED = 1


@dataclass
class Thresholds:
    """Tunable detection thresholds."""
    # An agent/source at or below this throughput is considered silent.
    silent_bytes_per_sec: float = 0.0
    # in->out loss percentages that raise warning / critical drop issues.
    drop_warn_pct: float = 20.0
    drop_crit_pct: float = 50.0
    # Ignore drop math below this input rate; tiny pipelines are too noisy.
    drop_min_input_bytes_per_sec: float = 100.0
    # A SecOps log type with no bytes for this long (but data earlier) is silent.
    # Allows for Cloud Monitoring delta-metric publication lag. This is a floor:
    # the effective threshold is max(floor, cadence_multiplier * typical gap).
    secops_silent_after_minutes: int = 90
    # Silent once the gap exceeds this multiple of the log type's median arrival gap,
    # so batch/context feeds (e.g. GCP_*_CONTEXT every few hours) aren't flagged.
    secops_cadence_multiplier: float = 3.0
    # Need this many non-zero buckets in the window before judging a log type.
    secops_min_buckets_for_cadence: int = 4
    # Log types never evaluated for silence (case-insensitive).
    secops_silent_ignore: Tuple[str, ...] = ()
    # Delivery (BindPlane -> SecOps) ratio drop vs 24h baseline, in percent.
    # Coarse by design: bytes are measured differently on each side.
    delivery_gap_warn_pct: float = 50.0
    delivery_gap_crit_pct: float = 80.0
    # Ignore log-type groups with less BindPlane volume than this in a window.
    delivery_min_bytes: float = 1_000_000.0
    # Flag a group whose 24h ratio is this % below the fleet-median ratio.
    delivery_baseline_outlier_pct: float = 75.0


@dataclass
class Issue:
    """One detected problem."""
    severity: str
    category: str
    entity_type: str  # "agent" | "configuration" | "log_type" | "integration"
    entity: str
    message: str
    evidence: Dict[str, Any] = field(default_factory=dict)
    first_seen: Optional[str] = None
    last_seen: Optional[str] = None

    @property
    def key(self) -> str:
        return f"{self.category}:{self.entity_type}:{self.entity}"

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["key"] = self.key
        return data


def sort_issues(issues: List[Issue]) -> List[Issue]:
    """Critical first, then warning, then info; stable by category/entity."""
    return sorted(issues, key=lambda i: (_SEVERITY_RANK.get(i.severity, 9), i.category, i.entity))


def count_by_severity(issues: List[Issue]) -> Dict[str, int]:
    counts = {SEVERITY_CRITICAL: 0, SEVERITY_WARNING: 0, SEVERITY_INFO: 0}
    for issue in issues:
        counts[issue.severity] = counts.get(issue.severity, 0) + 1
    return counts


def _parse_ts(value: str) -> Optional[datetime]:
    """Parse RFC3339 timestamps from Cloud Monitoring/BindPlane (Py3.9-safe)."""
    if not value:
        return None
    text = value.strip().replace("Z", "+00:00")
    # Trim fractional seconds to microseconds; fromisoformat on 3.9/3.10 is strict.
    if "." in text:
        head, _, tail = text.partition(".")
        frac = "".join(ch for ch in tail if ch.isdigit())
        tz = tail[len(frac):]
        text = f"{head}.{frac[:6].ljust(6, '0')}{tz}"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# BindPlane
# ---------------------------------------------------------------------------

def summarize_throughput(metrics: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """
    Collapse agentMetrics rows into per-agent source-in / destination-out B/s.

    Node IDs look like `source/source2_<ulid>`, `destination/<name>-0`, each
    optionally suffixed `/processors`. Only the un-suffixed nodes are counted so
    processor stages are not double-counted.
    """
    per_agent: Dict[str, Dict[str, Any]] = {}
    for row in metrics:
        if not str(row.get("name", "")).endswith("log_data_size"):
            continue
        agent_id = row.get("agentID") or ""
        node = str(row.get("nodeID") or "")
        if not agent_id or node.endswith("/processors"):
            continue
        value = float(row.get("value") or 0.0)
        entry = per_agent.setdefault(
            agent_id, {"source_bps": 0.0, "dest_bps": 0.0, "sources": {}, "destinations": {}}
        )
        if node.startswith("source/"):
            entry["source_bps"] += value
            entry["sources"][node] = entry["sources"].get(node, 0.0) + value
        elif node.startswith("destination/"):
            entry["dest_bps"] += value
            entry["destinations"][node] = entry["destinations"].get(node, 0.0) + value
    return per_agent


def _loss_pct(source_bps: float, dest_bps: float) -> Optional[float]:
    if source_bps <= 0:
        return None
    return round(max(0.0, (source_bps - dest_bps) / source_bps * 100.0), 1)


def _agent_label(agent: Dict[str, Any]) -> str:
    return agent.get("name") or agent.get("hostname") or agent.get("id", "unknown")


def _agent_config(agent: Dict[str, Any]) -> str:
    return (
        (agent.get("labels") or {}).get("configuration")
        or (agent.get("configurationStatus") or {}).get("assigned")
        or ""
    )


def evaluate_bindplane(
    agents: List[Dict[str, Any]],
    configurations: List[Dict[str, Any]],
    metrics: List[Dict[str, Any]],
    thresholds: Optional[Thresholds] = None,
) -> Tuple[List[Issue], List[Dict[str, Any]]]:
    """
    Apply BindPlane rules.

    Returns (issues, agent_rows) where agent_rows is a display-ready table.
    """
    t = thresholds or Thresholds()
    issues: List[Issue] = []
    throughput = summarize_throughput(metrics)
    out_of_date: List[str] = []
    rows: List[Dict[str, Any]] = []

    for agent in agents:
        agent_id = agent.get("id", "")
        label = _agent_label(agent)
        config = _agent_config(agent)
        status = agent.get("status")
        connected = bool(agent.get("connected")) and status != AGENT_STATUS_DISCONNECTED
        tp = throughput.get(agent_id)
        agent_issues: List[Issue] = []
        base_evidence = {"agent_id": agent_id, "configuration": config}

        if not connected:
            agent_issues.append(Issue(
                SEVERITY_CRITICAL, "AGENT_DISCONNECTED", "agent", label,
                f"Collector {label} is disconnected from BindPlane. Logs from its sources are not being collected.",
                {**base_evidence, "status": status, "disconnected_at": agent.get("disconnectedAt")},
            ))
        else:
            if status not in (AGENT_STATUS_CONNECTED, None):
                agent_issues.append(Issue(
                    SEVERITY_WARNING, "AGENT_STATUS_ABNORMAL", "agent", label,
                    f"Collector {label} reports non-standard status {status}. Check the agent in BindPlane.",
                    {**base_evidence, "status": status},
                ))

            if tp is None or tp["dest_bps"] <= t.silent_bytes_per_sec:
                agent_issues.append(Issue(
                    SEVERITY_CRITICAL, "AGENT_SILENT", "agent", label,
                    f"Collector {label} is connected but sent no data to SecOps in the window.",
                    {**base_evidence,
                     "source_bps": round(tp["source_bps"], 2) if tp else None,
                     "dest_bps": round(tp["dest_bps"], 2) if tp else None,
                     "reason": "no throughput reported" if tp is None else "zero destination throughput"},
                ))
            elif tp is not None:
                # Per-source silence only matters when the agent itself is alive.
                for node, value in sorted(tp["sources"].items()):
                    if value <= t.silent_bytes_per_sec and tp["source_bps"] > t.silent_bytes_per_sec:
                        agent_issues.append(Issue(
                            SEVERITY_WARNING, "SOURCE_SILENT", "agent", f"{label} / {node}",
                            f"Source {node} on {label} received no data while other sources on the same collector did.",
                            {**base_evidence, "node_id": node, "bps": value},
                        ))

                loss = _loss_pct(tp["source_bps"], tp["dest_bps"])
                if loss is not None and tp["source_bps"] >= t.drop_min_input_bytes_per_sec and loss >= t.drop_warn_pct:
                    severity = SEVERITY_CRITICAL if loss >= t.drop_crit_pct else SEVERITY_WARNING
                    agent_issues.append(Issue(
                        severity, "PIPELINE_DROP", "agent", label,
                        f"Collector {label} sent {loss}% less data to SecOps than it received. "
                        "Check exporter errors, queue overflow, or processors that filter on purpose.",
                        {**base_evidence, "source_bps": round(tp["source_bps"], 2),
                         "dest_bps": round(tp["dest_bps"], 2), "loss_pct": loss},
                    ))

        if agent.get("remoteConfigStatus") == OPAMP_REMOTE_CONFIG_FAILED:
            agent_issues.append(Issue(
                SEVERITY_CRITICAL, "AGENT_CONFIG_FAILED", "agent", label,
                f"Collector {label} failed to apply its configuration ({config}).",
                {**base_evidence, "remote_config_status": agent.get("remoteConfigStatus")},
            ))

        health_status = agent.get("healthStatus")
        if health_status not in (0, None):
            agent_issues.append(Issue(
                SEVERITY_WARNING, "AGENT_UNHEALTHY", "agent", label,
                f"Collector {label} reports healthStatus={health_status}.",
                {**base_evidence, "health_status": health_status},
            ))

        if agent.get("outOfDate"):
            out_of_date.append(label)

        issues.extend(agent_issues)
        worst = sort_issues(agent_issues)[0].severity if agent_issues else "ok"
        rows.append({
            "id": agent_id,
            "name": label,
            "configuration": config,
            "connected": connected,
            "status": status,
            "version": agent.get("version"),
            "platform": agent.get("platform"),
            "out_of_date": bool(agent.get("outOfDate")),
            "source_bps": round(tp["source_bps"], 2) if tp else 0.0,
            "dest_bps": round(tp["dest_bps"], 2) if tp else 0.0,
            "loss_pct": _loss_pct(tp["source_bps"], tp["dest_bps"]) if tp else None,
            "health": worst,
        })

    for cfg in configurations:
        name = (cfg.get("metadata") or {}).get("name", "unknown")
        rollout = (cfg.get("status") or {}).get("rollout") or {}
        errors = int((rollout.get("progress") or {}).get("errors") or 0)
        if errors > 0:
            issues.append(Issue(
                SEVERITY_CRITICAL, "CONFIG_ROLLOUT_ERROR", "configuration", name,
                f"Configuration {name} rollout has {errors} agent error(s).",
                {"rollout_status": rollout.get("status"), "progress": rollout.get("progress")},
            ))

    if out_of_date:
        # One aggregate issue: per-agent "upgrade available" rows are noise.
        issues.append(Issue(
            SEVERITY_INFO, "AGENTS_OUT_OF_DATE", "integration", "bindplane",
            f"{len(out_of_date)} of {len(agents)} collectors have an upgrade available.",
            {"agents": sorted(out_of_date)},
        ))

    rows.sort(key=lambda r: (_SEVERITY_RANK.get(r["health"], 3), r["name"]))
    return sort_issues(issues), rows


# ---------------------------------------------------------------------------
# SecOps
# ---------------------------------------------------------------------------

def evaluate_secops_silence(
    bytes_series: List[Dict[str, Any]],
    now: datetime,
    thresholds: Optional[Thresholds] = None,
) -> List[Issue]:
    """
    Flag SecOps log types that had data earlier in the window but none recently.

    Uses the latest non-zero bucket per log_type rather than counting zero
    buckets, because Cloud Monitoring omits points entirely when there is no data.
    """
    t = thresholds or Thresholds()
    ignored = {lt.upper() for lt in t.secops_silent_ignore}
    seen_times: Dict[str, set] = {}
    for ts in bytes_series:
        log_type = ((ts.get("resource") or {}).get("labels") or {}).get("log_type", "UNKNOWN")
        for pt in ts.get("points", []) or []:
            val = pt.get("value") or {}
            if int(val.get("int64Value", 0) or val.get("doubleValue", 0) or 0) <= 0:
                continue
            end = _parse_ts((pt.get("interval") or {}).get("endTime", ""))
            if end:
                seen_times.setdefault(log_type, set()).add(end)

    issues = []
    for log_type, times in sorted(seen_times.items()):
        if log_type.upper() in ignored:
            continue
        ordered = sorted(times)
        # Too few arrivals to infer a cadence: a feed seen once or twice a day is
        # almost certainly batch, and flagging it would be noise.
        if len(ordered) < t.secops_min_buckets_for_cadence:
            continue

        gaps = sorted(
            (b - a).total_seconds() / 60 for a, b in zip(ordered, ordered[1:])
        )
        median_gap = gaps[len(gaps) // 2]
        threshold = max(t.secops_silent_after_minutes, t.secops_cadence_multiplier * median_gap)

        last = ordered[-1]
        silent_minutes = int((now - last).total_seconds() // 60)
        if silent_minutes > threshold:
            issues.append(Issue(
                SEVERITY_CRITICAL, "SECOPS_LOG_TYPE_SILENT", "log_type", log_type,
                f"SecOps has received no {log_type} data for {silent_minutes} minutes "
                f"(normally arrives every ~{int(median_gap)} min).",
                {"last_data_at": last.strftime("%Y-%m-%dT%H:%M:%SZ"),
                 "silent_minutes": silent_minutes,
                 "typical_gap_minutes": int(median_gap),
                 "threshold_minutes": int(threshold)},
            ))
    return issues


def evaluate_secops_parsing(log_types: List[Any]) -> List[Issue]:
    """
    Surface parser problems from the existing calculator health statuses.

    Only PARSER_FAILURE (records arriving, zero events) is critical. CRITICAL_DROPS
    is downgraded to a warning because records-vs-events is a heuristic: one raw
    record can legitimately yield zero or several UDM events.
    """
    issues = []
    for src in log_types:
        status = getattr(src, "health_status", None)
        name = getattr(src, "log_type", "UNKNOWN")
        if status == "PARSER_FAILURE":
            issues.append(Issue(
                SEVERITY_CRITICAL, "SECOPS_PARSER_FAILURE", "log_type", name,
                f"{name}: logs are arriving but no UDM events are produced. Check the parser.",
                {"record_count": src.record_count, "normalized_event_count": src.normalized_event_count},
            ))
        elif status == "CRITICAL_DROPS":
            issues.append(Issue(
                SEVERITY_WARNING, "SECOPS_NORMALIZATION_LOW", "log_type", name,
                f"{name}: only {src.normalization_rate_pct}% of records normalized.",
                {"normalization_rate_pct": src.normalization_rate_pct},
            ))
    return issues


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

class IssueTracker:
    """
    In-memory first-seen / recently-resolved bookkeeping.

    Per-process: with multiple Gunicorn workers each keeps its own view, and
    state resets on restart. Acceptable until issues are persisted (phase 3).
    """

    def __init__(self, resolved_retention_seconds: int = 3600):
        self._active: Dict[str, Issue] = {}
        self._resolved: Dict[str, Issue] = {}
        self._retention = resolved_retention_seconds
        self._lock = Lock()

    def update(self, issues: List[Issue], now: datetime) -> Tuple[List[Issue], List[Issue]]:
        """Record the current issue set; return (active, recently_resolved)."""
        stamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        with self._lock:
            current: Dict[str, Issue] = {}
            for issue in issues:
                previous = self._active.get(issue.key) or self._resolved.pop(issue.key, None)
                issue.first_seen = previous.first_seen if previous and issue.key in self._active else stamp
                issue.last_seen = stamp
                current[issue.key] = issue

            for key, old in self._active.items():
                if key not in current:
                    old.last_seen = stamp
                    self._resolved[key] = old

            cutoff = now.timestamp() - self._retention
            self._resolved = {
                k: v for k, v in self._resolved.items()
                if (_parse_ts(v.last_seen or "") or now).timestamp() >= cutoff
            }
            self._active = current
            return sort_issues(list(current.values())), sort_issues(list(self._resolved.values()))

    def reset(self) -> None:
        with self._lock:
            self._active.clear()
            self._resolved.clear()
