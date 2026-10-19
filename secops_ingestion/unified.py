"""
Unified log-type view: BindPlane | Delivery | SecOps side by side.

Author: Joshua Koch
Created: 2026-10-19
Last Updated: 2026-10-19
Version: 1.0.0
Description: Joins BindPlane collector throughput, delivery reconciliation, and
             SecOps (Cloud Monitoring) ingestion/parsing metrics into one row
             per log type (or per multi-log-type BindPlane group), with a
             per-collector drill-down and a 24h SecOps sparkline.

Row model
---------
* BindPlane groups come first (a source that emits several log types is one
  row, because BindPlane bytes cannot be split between them).
* Every SecOps log type not covered by a BindPlane group gets its own row, so
  GCP-direct and feed-based log types appear too, with no BindPlane columns.
"""
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .delivery import GroupTotals, LogTypeGroup
from .pipeline_health import Issue, _SEVERITY_RANK, _parse_ts

# Observed fixed SecOps collector_ids per ingestion path. Not documented by
# Google; unknown ids are shown as "Other".
OBSERVED_COLLECTOR_PATHS = {
    "aaaa1111-aaaa-1111-aaaa-1111aaaa1112": "BindPlane",
    "aaaa3333-aaaa-3333-aaaa-3333aaaa3333": "GCP direct",
    "dddddddd-dddd-dddd-dddd-dddddddddddd": "Feed / context",
}

_DELIVERY_SEVERITY = {
    "stopped": "critical", "gap": "warning", "gap_24h": "warning", "missing": "warning",
}
_PARSER_SEVERITY = {
    "PARSER_FAILURE": "critical", "CRITICAL_DROPS": "warning", "DEGRADED": "warning",
}


def path_for_collector(collector_id: str, bindplane_ids: Iterable[str]) -> str:
    if collector_id in set(bindplane_ids):
        return "BindPlane"
    return OBSERVED_COLLECTOR_PATHS.get(collector_id, "Other")


def _worst(*severities: Optional[str]) -> str:
    present = [s for s in severities if s in _SEVERITY_RANK]
    return min(present, key=lambda s: _SEVERITY_RANK[s]) if present else "ok"


def secops_series_by_log_type(
    series: List[Dict[str, Any]],
) -> Tuple[List[str], Dict[str, Dict[str, float]], Dict[str, Dict[str, float]]]:
    """
    Index a bytes timeSeries list.

    Returns (sorted bucket end times, {log_type: {endTime: bytes}},
             {log_type: {collector_id: bytes}}).
    """
    buckets: set = set()
    by_time: Dict[str, Dict[str, float]] = {}
    by_collector: Dict[str, Dict[str, float]] = {}
    for ts in series:
        labels = (ts.get("resource") or {}).get("labels") or {}
        log_type = labels.get("log_type", "UNKNOWN")
        collector = labels.get("collector_id", "unknown")
        for pt in ts.get("points", []) or []:
            end = (pt.get("interval") or {}).get("endTime", "")
            val = pt.get("value") or {}
            v = float(val.get("int64Value", 0) or val.get("doubleValue", 0) or 0)
            if end:
                buckets.add(end)
                by_time.setdefault(log_type, {})[end] = by_time.get(log_type, {}).get(end, 0.0) + v
            by_collector.setdefault(log_type, {})[collector] = by_collector.get(log_type, {}).get(collector, 0.0) + v
    return sorted(buckets, key=lambda t: _parse_ts(t) or t), by_time, by_collector


def _sum_window(points: Dict[str, float], since, until) -> float:
    total = 0.0
    for end, v in points.items():
        ts = _parse_ts(end)
        if ts is not None and since < ts <= until:
            total += v
    return total


def build_unified_rows(
    groups_recent: Dict[LogTypeGroup, GroupTotals],
    groups_baseline: Dict[LogTypeGroup, GroupTotals],
    delivery_rows: List[Dict[str, Any]],
    secops_bytes_series: List[Dict[str, Any]],
    secops_log_types: List[Any],
    agents_table: List[Dict[str, Any]],
    issues: List[Issue],
    bindplane_collector_ids: Iterable[str],
    recent_since,
    recent_until,
) -> List[Dict[str, Any]]:
    """Produce display rows; worst status first, then by SecOps 24h volume."""
    bp_ids = tuple(bindplane_collector_ids)
    bucket_times, bytes_by_time, bytes_by_collector = secops_series_by_log_type(secops_bytes_series)
    secops_meta = {getattr(s, "log_type", ""): s for s in secops_log_types}
    delivery_by_group = {r["group"]: r for r in delivery_rows}
    agent_rows = {a["name"]: a for a in agents_table}

    groups: List[LogTypeGroup] = sorted(set(groups_recent) | set(groups_baseline))
    covered = {lt for g in groups for lt in g}
    secops_only = sorted(
        lt for lt in set(secops_meta) | set(bytes_by_time) if lt not in covered
    )

    rows = []
    for group in groups + [(lt,) for lt in secops_only]:
        label = " + ".join(group)
        is_bp_group = group in groups_recent or group in groups_baseline

        # ---- SecOps side (summed across the group's log types) ----------------
        sparkline = [
            sum(bytes_by_time.get(lt, {}).get(t, 0.0) for lt in group) for t in bucket_times
        ]
        paths: Dict[str, float] = {}
        for lt in group:
            for collector, v in bytes_by_collector.get(lt, {}).items():
                path = path_for_collector(collector, bp_ids)
                paths[path] = paths.get(path, 0.0) + v
        bytes_24h = records = normalized = 0.0
        parser_statuses = []
        per_log_type = []
        for lt in group:
            meta = secops_meta.get(lt)
            lt_bytes = float(getattr(meta, "bytes_count", 0) or 0)
            lt_records = float(getattr(meta, "record_count", 0) or 0)
            lt_norm = float(getattr(meta, "normalized_event_count", 0) or 0)
            bytes_24h += lt_bytes
            records += lt_records
            normalized += lt_norm
            status = getattr(meta, "health_status", None)
            if status:
                parser_statuses.append(status)
            per_log_type.append({
                "log_type": lt,
                "bytes_24h": int(lt_bytes),
                "records_24h": int(lt_records),
                "normalization_rate_pct": getattr(meta, "normalization_rate_pct", None),
                "health_status": status,
            })
        received_recent = sum(_sum_window(bytes_by_time.get(lt, {}), recent_since, recent_until) for lt in group)
        parser_severity = _worst(*(_PARSER_SEVERITY.get(s) for s in parser_statuses))
        norm_pct = round(normalized / records * 100.0, 2) if records > 0 else None

        # ---- BindPlane side -----------------------------------------------------
        collectors = []
        if is_bp_group:
            recent = groups_recent.get(group, GroupTotals())
            base = groups_baseline.get(group, GroupTotals())
            names = sorted(set(recent.bytes_by_agent) | set(base.bytes_by_agent))
            for name in names:
                info = agent_rows.get(name, {})
                collectors.append({
                    "name": name,
                    "bps_recent": round(recent.bytes_by_agent.get(name, 0.0) / 3600.0, 1),
                    "share_24h_pct": round(base.bytes_by_agent.get(name, 0.0) / base.bytes * 100.0, 1) if base.bytes else 0.0,
                    "connected": info.get("connected"),
                    "health": info.get("health", "ok"),
                    "configuration": info.get("configuration"),
                })
            collectors.sort(key=lambda c: (_SEVERITY_RANK.get(c["health"], 3), -c["share_24h_pct"]))
            bp_sent_recent = recent.bytes
            bp_configs = sorted(recent.configurations | base.configurations)
        else:
            bp_sent_recent = None
            bp_configs = []
        silent_collectors = sum(1 for c in collectors if c["bps_recent"] <= 0)
        bp_severity = _worst(*(c["health"] for c in collectors if c["share_24h_pct"] > 0 or c["bps_recent"] > 0))

        # ---- Delivery ---------------------------------------------------------
        delivery = delivery_by_group.get(label)
        delivery_severity = _DELIVERY_SEVERITY.get((delivery or {}).get("status"))

        # ---- Related issues ---------------------------------------------------
        collector_names = {c["name"] for c in collectors}
        related = []
        for issue in issues:
            if issue.entity_type == "log_type" and (issue.entity == label or issue.entity in group):
                related.append(issue)
            elif issue.entity_type == "agent":
                agent_name, _, suffix = issue.entity.partition(" / ")
                if suffix and not suffix.startswith("source/"):
                    # Source-scoped issue ("agent / LOGTYPE"): belongs to that row only.
                    if suffix == label:
                        related.append(issue)
                elif agent_name in collector_names:
                    related.append(issue)
        issue_severity = _worst(*(i.severity for i in related if i.severity != "info"))

        if not is_bp_group and bytes_24h <= 0 and records <= 0 and not related:
            continue  # SecOps-only log type with no data and nothing to report

        rows.append({
            "key": label,
            "log_types": list(group),
            "label": label,
            "paths": sorted(paths, key=lambda p: -paths[p]) or (["BindPlane"] if is_bp_group else []),
            "status": _worst(bp_severity, delivery_severity, parser_severity, issue_severity),
            "bindplane": None if not is_bp_group else {
                "collector_count": len(collectors),
                "silent_collectors": silent_collectors,
                "sent_bytes_recent": int(bp_sent_recent or 0),
                "configurations": bp_configs,
                "status": bp_severity,
                "collectors": collectors,
            },
            "delivery": None if not delivery else {
                "status": delivery["status"],
                "ratio_recent": delivery.get("ratio_recent"),
                "ratio_baseline": delivery.get("ratio_baseline"),
                "ratio_change_pct": delivery.get("ratio_change_pct"),
            },
            "secops": {
                "received_bytes_recent": int(received_recent),
                "bytes_24h": int(bytes_24h),
                "records_24h": int(records),
                "normalized_24h": int(normalized),
                "normalization_rate_pct": norm_pct,
                "parser_status": parser_severity,
                "paths_bytes_24h": {p: int(v) for p, v in paths.items()},
                "per_log_type": per_log_type,
                "sparkline": [int(v) for v in sparkline],
            },
            "issues": [{"severity": i.severity, "category": i.category, "message": i.message} for i in related],
        })

    rows.sort(key=lambda r: (_SEVERITY_RANK.get(r["status"], 3), -r["secops"]["bytes_24h"]))
    return rows
