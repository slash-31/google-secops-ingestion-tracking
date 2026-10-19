"""
BindPlane -> SecOps delivery reconciliation.

Author: Joshua Koch
Created: 2026-10-19
Last Updated: 2026-10-19
Version: 1.0.0
Description: Compares what BindPlane collectors report sending against what
             SecOps reports receiving, per SecOps log type, to detect logs lost
             between the two systems.

Why ratios, not equality
------------------------
BindPlane measures OTLP payload size (uncompressed, with attributes); SecOps
counts raw ingested bytes. In Argolis the two differ by roughly 3x. Absolute
comparison is meaningless, so each log-type group is judged against its own
24h SecOps/BindPlane ratio. A sharp drop in the recent ratio means SecOps is
receiving much less than usual relative to what BindPlane is sending.

Why log-type groups
-------------------
SecOps tags all BindPlane traffic with one fixed collector_id, so individual
collectors cannot be told apart on the SecOps side. Log type is the only
shared key. A source that assigns several log types conditionally (e.g. a
Windows Event source -> WINEVTLOG / WINDOWS_SYSMON / WINDOWS_DEFENDER_AV) is
treated as one group compared against the sum of those log types.
"""
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from .pipeline_health import (
    Issue,
    SEVERITY_CRITICAL,
    SEVERITY_WARNING,
    Thresholds,
    _parse_ts,
)

# Observed fixed collector_id SecOps assigns to BindPlane (Chronicle exporter)
# traffic. Overridable because Google could change it.
DEFAULT_BINDPLANE_COLLECTOR_IDS = ("aaaa1111-aaaa-1111-aaaa-1111aaaa1112",)

_STANDARDIZATION_PREFIX = "google_secops_standardization"

LogTypeGroup = Tuple[str, ...]


@dataclass
class SourceInfo:
    """A configured BindPlane source and the SecOps log types it emits."""
    source_id: str
    configuration: str
    source_type: str
    log_types: LogTypeGroup
    filters: bool = False  # has non-standardization processors (may drop on purpose)


@dataclass
class GroupTotals:
    bytes: float = 0.0
    agents: Set[str] = field(default_factory=set)
    configurations: Set[str] = field(default_factory=set)
    bytes_by_agent: Dict[str, float] = field(default_factory=dict)

    def top_agents(self, limit: int = 3) -> List[Dict[str, Any]]:
        """Largest contributors with their share of the group's BindPlane bytes."""
        ranked = sorted(self.bytes_by_agent.items(), key=lambda kv: -kv[1])[:limit]
        return [
            {"agent": name, "bytes": int(val),
             "share_pct": round(val / self.bytes * 100.0, 1) if self.bytes else 0.0}
            for name, val in ranked
        ]


def _extract_log_types(processors: Optional[List[Dict[str, Any]]]) -> List[str]:
    found: List[str] = []
    for proc in processors or []:
        if not str(proc.get("type", "")).startswith(_STANDARDIZATION_PREFIX):
            continue
        for param in proc.get("parameters", []) or []:
            if param.get("name") != "secops_field":
                continue
            for entry in param.get("value") or []:
                log_type = (entry or {}).get("logType")
                if log_type and log_type not in found:
                    found.append(log_type)
    return found


def map_sources(configurations: List[Dict[str, Any]]) -> Dict[str, SourceInfo]:
    """Map BindPlane source id -> SourceInfo (only sources that set a SecOps log type)."""
    mapping: Dict[str, SourceInfo] = {}
    for cfg in configurations:
        cfg_name = (cfg.get("metadata") or {}).get("name", "")
        for src in (cfg.get("spec") or {}).get("sources", []) or []:
            source_id = src.get("id")
            log_types = _extract_log_types(src.get("processors"))
            if not source_id or not log_types:
                continue
            filters = any(
                not str(p.get("type", "")).startswith(_STANDARDIZATION_PREFIX)
                for p in src.get("processors") or []
            )
            mapping[source_id] = SourceInfo(
                source_id=source_id,
                configuration=cfg_name,
                source_type=src.get("type") or src.get("name") or "",
                log_types=tuple(sorted(log_types)),
                filters=filters,
            )
    return mapping


def parse_source_node(node_id: str) -> Tuple[Optional[str], bool]:
    """
    'source/source2_<id>[/processors]' -> (<id>, is_processors_node).

    Library sources look like 'source/Okta_Ingestion_<id>'; the id is always
    the segment after the last underscore.
    """
    if not node_id.startswith("source/"):
        return None, False
    body = node_id[len("source/"):]
    is_proc = body.endswith("/processors")
    if is_proc:
        body = body[: -len("/processors")]
    if "_" not in body:
        return None, is_proc
    return body.rsplit("_", 1)[1], is_proc


def bindplane_bytes_by_group(
    metrics: List[Dict[str, Any]],
    sources: Dict[str, SourceInfo],
    window_seconds: int,
    agent_names: Optional[Dict[str, str]] = None,
) -> Dict[LogTypeGroup, GroupTotals]:
    """
    Convert per-source B/s into bytes per log-type group over the window.

    Uses the post-processor node (after filters and standardization) when
    present, since that is what actually heads to the destination.
    """
    per_source: Dict[Tuple[str, str], Dict[str, float]] = {}
    for row in metrics:
        if not str(row.get("name", "")).endswith("log_data_size"):
            continue
        source_id, is_proc = parse_source_node(str(row.get("nodeID") or ""))
        agent_id = row.get("agentID") or ""
        if not source_id or source_id not in sources or not agent_id:
            continue
        slot = per_source.setdefault((agent_id, source_id), {})
        slot["proc" if is_proc else "raw"] = float(row.get("value") or 0.0)

    groups: Dict[LogTypeGroup, GroupTotals] = {}
    for (agent_id, source_id), vals in per_source.items():
        bps = vals.get("proc", vals.get("raw", 0.0))
        info = sources[source_id]
        totals = groups.setdefault(info.log_types, GroupTotals())
        contributed = bps * window_seconds
        name = (agent_names or {}).get(agent_id, agent_id)
        totals.bytes += contributed
        totals.bytes_by_agent[name] = totals.bytes_by_agent.get(name, 0.0) + contributed
        totals.agents.add(agent_id)
        totals.configurations.add(info.configuration)
    return groups


def secops_bytes_by_log_type(
    series: List[Dict[str, Any]],
    collector_ids: Iterable[str],
    since: Optional[datetime] = None,
    until: Optional[datetime] = None,
) -> Dict[str, float]:
    """
    Sum SecOps bytes per log_type for the given collector_ids.

    Points are included when since < interval.endTime <= until (either bound optional).
    """
    wanted = set(collector_ids)
    totals: Dict[str, float] = {}
    for ts in series:
        labels = (ts.get("resource") or {}).get("labels") or {}
        if labels.get("collector_id") not in wanted:
            continue
        log_type = labels.get("log_type", "UNKNOWN")
        for pt in ts.get("points", []) or []:
            if since is not None or until is not None:
                end = _parse_ts((pt.get("interval") or {}).get("endTime", ""))
                if end is None:
                    continue
                if since is not None and end <= since:
                    continue
                if until is not None and end > until:
                    continue
            val = pt.get("value") or {}
            totals[log_type] = totals.get(log_type, 0.0) + float(
                val.get("int64Value", 0) or val.get("doubleValue", 0) or 0
            )
    return totals


def fleet_baseline_ratio(
    bp_baseline: Dict[LogTypeGroup, "GroupTotals"],
    secops_baseline: Dict[str, float],
    min_bytes: float,
    min_groups: int = 3,
) -> Optional[float]:
    """
    Median 24h SecOps/BindPlane ratio across log-type groups with real volume.

    Serves as the "normal" byte-accounting ratio for this deployment. None when
    too few groups exist to make the median meaningful.
    """
    ratios = []
    for group, totals in bp_baseline.items():
        if totals.bytes < min_bytes:
            continue
        received = sum(secops_baseline.get(lt, 0.0) for lt in group)
        if received > 0:
            ratios.append(received / totals.bytes)
    if len(ratios) < min_groups:
        return None
    ratios.sort()
    mid = len(ratios) // 2
    median = ratios[mid] if len(ratios) % 2 else (ratios[mid - 1] + ratios[mid]) / 2
    return round(median, 4)


def _ratio(numerator: float, denominator: float) -> Optional[float]:
    return round(numerator / denominator, 4) if denominator > 0 else None


def _group_label(group: LogTypeGroup) -> str:
    return " + ".join(group)


def evaluate_delivery(
    bp_recent: Dict[LogTypeGroup, GroupTotals],
    bp_baseline: Dict[LogTypeGroup, GroupTotals],
    secops_recent: Dict[str, float],
    secops_baseline: Dict[str, float],
    thresholds: Optional[Thresholds] = None,
) -> Tuple[List[Issue], List[Dict[str, Any]]]:
    """
    Compare recent vs baseline SecOps/BindPlane ratios per log-type group.

    Rules:
      DELIVERY_STOPPED    BindPlane sent >= min bytes recently, SecOps got nothing.
      DELIVERY_GAP        recent ratio fell >= warn/crit % below the 24h baseline.
      DELIVERY_GAP_24H    the group's 24h ratio is far below the fleet-wide median,
                          i.e. a burst BindPlane sent that SecOps never received.
      LOG_TYPE_NOT_IN_SECOPS  BindPlane sent data all day under a log type SecOps
                              never received (wrong/unknown log type, or rejected).
    """
    t = thresholds or Thresholds()
    issues: List[Issue] = []
    rows: List[Dict[str, Any]] = []
    fleet_median = fleet_baseline_ratio(bp_baseline, secops_baseline, t.delivery_min_bytes)

    for group in sorted(set(bp_recent) | set(bp_baseline)):
        label = _group_label(group)
        recent = bp_recent.get(group, GroupTotals())
        base = bp_baseline.get(group, GroupTotals())
        s_recent = sum(secops_recent.get(lt, 0.0) for lt in group)
        s_base = sum(secops_baseline.get(lt, 0.0) for lt in group)
        r_recent = _ratio(s_recent, recent.bytes)
        r_base = _ratio(s_base, base.bytes)
        change_pct = (
            round((r_recent - r_base) / r_base * 100.0, 1)
            if r_recent is not None and r_base else None
        )
        evidence = {
            "log_types": list(group),
            "bindplane_bytes_recent": int(recent.bytes),
            "secops_bytes_recent": int(s_recent),
            "bindplane_bytes_baseline": int(base.bytes),
            "secops_bytes_baseline": int(s_base),
            "ratio_recent": r_recent,
            "ratio_baseline": r_base,
            "ratio_change_pct": change_pct,
            "configurations": sorted(recent.configurations | base.configurations),
            "fleet_ratio_baseline": fleet_median,
            "top_agents_recent": recent.top_agents(),
            "top_agents_baseline": base.top_agents(),
        }
        status = "ok"
        baseline_outlier = (
            fleet_median is not None and r_base is not None and base.bytes >= t.delivery_min_bytes
            and r_base < fleet_median * (1 - t.delivery_baseline_outlier_pct / 100.0)
        )

        if base.bytes >= t.delivery_min_bytes and s_base <= 0:
            status = "missing"
            issues.append(Issue(
                SEVERITY_WARNING, "LOG_TYPE_NOT_IN_SECOPS", "log_type", label,
                f"BindPlane sent {base.bytes / 1e6:,.1f} MB of {label} in 24h but SecOps "
                "recorded none under that log type. Check the log type name and SecOps ingestion errors.",
                evidence,
            ))
        elif recent.bytes >= t.delivery_min_bytes and s_recent <= 0 and s_base > 0:
            status = "stopped"
            issues.append(Issue(
                SEVERITY_CRITICAL, "DELIVERY_STOPPED", "log_type", label,
                f"BindPlane sent {recent.bytes / 1e6:,.1f} MB of {label} in the last hour "
                "but SecOps received none.",
                evidence,
            ))
        elif (recent.bytes >= t.delivery_min_bytes and r_recent is not None
              and r_base and change_pct is not None and -change_pct >= t.delivery_gap_warn_pct):
            drop = -change_pct
            severity = SEVERITY_CRITICAL if drop >= t.delivery_gap_crit_pct else SEVERITY_WARNING
            status = "gap"
            issues.append(Issue(
                severity, "DELIVERY_GAP", "log_type", label,
                f"SecOps is receiving {drop}% less {label} than usual relative to what "
                "BindPlane is sending. Possible loss between BindPlane and SecOps.",
                evidence,
            ))
        elif baseline_outlier:
            # The group's own 24h baseline is polluted (e.g. a burst BindPlane sent
            # that SecOps never received), so compare it to the fleet instead.
            status = "gap_24h"
            expected = base.bytes * fleet_median
            top = base.top_agents(1)
            culprit = (f" {top[0]['agent']} accounts for {top[0]['share_pct']}% of BindPlane's {label} volume."
                       if top else "")
            issues.append(Issue(
                SEVERITY_WARNING, "DELIVERY_GAP_24H", "log_type", label,
                f"Over 24h SecOps received {s_base / 1e6:,.1f} MB of {label}; based on "
                f"BindPlane volume and the fleet-wide ratio, about {expected / 1e6:,.0f} MB was expected."
                f"{culprit} Check for a burst that was dropped (exporter retries/queue overflow).",
                evidence,
            ))
        elif recent.bytes < t.delivery_min_bytes:
            status = "low_volume"

        rows.append({"group": label, "status": status, **evidence})

    order = {"stopped": 0, "gap": 1, "gap_24h": 2, "missing": 3, "ok": 4, "low_volume": 5}
    rows.sort(key=lambda r: (order.get(r["status"], 9), -r["bindplane_bytes_recent"]))
    return issues, rows


def detect_idle_sources(
    agents: List[Dict[str, Any]],
    sources: Dict[str, SourceInfo],
    baseline_metrics: List[Dict[str, Any]],
) -> List[Issue]:
    """
    Flag configured sources that reported no throughput rows at all in the
    baseline window on agents that are otherwise reporting.

    Distinct from SOURCE_SILENT (node present at 0 B/s): here BindPlane has no
    measurement for the source, e.g. a syslog listener nobody is sending to.
    """
    reporting: Dict[str, Set[str]] = {}
    for row in baseline_metrics:
        source_id, _ = parse_source_node(str(row.get("nodeID") or ""))
        agent_id = row.get("agentID") or ""
        if agent_id:
            reporting.setdefault(agent_id, set())
            if source_id:
                reporting[agent_id].add(source_id)

    by_config: Dict[str, List[SourceInfo]] = {}
    for info in sources.values():
        by_config.setdefault(info.configuration, []).append(info)

    issues: List[Issue] = []
    for agent in agents:
        agent_id = agent.get("id", "")
        if agent_id not in reporting:
            continue  # no data at all; AGENT_SILENT/DISCONNECTED already covers it
        config = (agent.get("labels") or {}).get("configuration", "")
        label = agent.get("name") or agent_id
        for info in by_config.get(config, []):
            if info.source_id not in reporting[agent_id]:
                issues.append(Issue(
                    SEVERITY_WARNING, "SOURCE_NO_DATA_24H", "agent",
                    f"{label} / {_group_label(info.log_types)}",
                    f"Source for {_group_label(info.log_types)} ({info.source_type}) on {label} "
                    "reported no data in 24h. Confirm devices are sending to it, or remove it from the config.",
                    {"agent_id": agent_id, "configuration": config, "source_id": info.source_id},
                ))
    return issues


def recent_window(query_end: datetime, lag_minutes: int = 30, minutes: int = 60) -> Tuple[datetime, datetime]:
    """
    (since, until) bounds for the SecOps 'recent' window.

    Shifted back by `lag_minutes` because the newest 30m bucket is usually
    still filling (ingestion + metric publication lag); including it would
    make every log type look like it is losing data.
    """
    until = query_end - timedelta(minutes=lag_minutes)
    return until - timedelta(minutes=minutes), until
