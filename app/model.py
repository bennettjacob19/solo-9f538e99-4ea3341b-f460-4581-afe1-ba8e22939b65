"""Normalise and validate drill payloads.

Canonical shapes (JSON friendly):

Listening entry:
    {"source": "10.0.0.1" | None, "group": "239.1.1.1",
     "ports": ["1", "2", "3"]}

Traffic flow:
    {"source": "10.0.0.1", "group": "239.1.1.1", "frames": 120}

Hardware rule:
    {"source": None | "10.0.0.1", "group": "239.1.1.1",
     "action": "ports" | "cpu", "ports": ["1", "2"]}

Limits:
    {"hw": 10, "cpu": 1000}
"""
from __future__ import annotations

import ipaddress

from .netutils import (
    MAX_ROWS,
    ValidationError,
    is_multicast,
    parse_ipv4,
    parse_nonneg_int,
    parse_ports,
    parse_source,
)


def _row_get(row, *names):
    for name in names:
        if name in row and row[name] is not None:
            return row[name]
    return None


def normalize_listening(rows) -> tuple[list[dict], list[dict]]:
    issues: list[dict] = []
    entries: list[dict] = []
    seen: dict[tuple, int] = {}
    if rows is None:
        return entries, issues
    if not isinstance(rows, list):
        raise ValidationError("监听表必须是行数组")
    if len(rows) > MAX_ROWS:
        issues.append({"scope": "listening", "message": f"监听表最多 {MAX_ROWS} 行"})
        return entries, issues

    for idx, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            issues.append({"scope": "listening", "row": idx, "message": "行格式错误"})
            continue
        raw_source = _row_get(row, "source", "src", "s")
        raw_group = _row_get(row, "group", "g", "grp")
        raw_ports = _row_get(row, "ports", "receivers", "receiver_ports", "p")

        # A completely blank row is skipped (dynamic web forms carry empties).
        if _is_blank(raw_source) and _is_blank(raw_group) and _is_blank(raw_ports):
            continue

        row_issues_before = len(issues)
        source = None
        group = None
        ports: list[str] = []
        try:
            source = parse_source(raw_source)
        except ValueError as exc:
            issues.append({"scope": "listening", "row": idx, "field": "source", "message": str(exc)})
        try:
            group = parse_ipv4(raw_group, "组地址")
            if not is_multicast(group):
                issues.append({
                    "scope": "listening", "row": idx, "field": "group",
                    "message": f"组地址 {group} 不在 IPv4 组播段 224.0.0.0/4",
                })
        except ValueError as exc:
            issues.append({"scope": "listening", "row": idx, "field": "group", "message": str(exc)})
        try:
            ports = parse_ports(raw_ports if raw_ports is not None else [])
        except ValueError as exc:
            issues.append({"scope": "listening", "row": idx, "field": "ports", "message": str(exc)})

        if len(issues) > row_issues_before:
            continue

        key = (source, group)
        if key in seen:
            kind = f"({source}, {group})" if source else f"(*, {group})"
            issues.append({
                "scope": "listening", "row": idx, "field": "group",
                "message": f"监听表项 {kind} 与第 {seen[key]} 行重复，请合并接收端口",
            })
            continue
        seen[key] = idx
        entries.append({"source": source, "group": group, "ports": ports})

    # Stable order: group numeric, any-source before specific sources.
    entries.sort(key=lambda e: (int(ipaddress.IPv4Address(e["group"])),
                                e["source"] is not None,
                                e["source"] or ""))
    return entries, issues


def normalize_traffic(rows) -> tuple[list[dict], list[dict]]:
    issues: list[dict] = []
    flows: list[dict] = []
    if rows is None:
        return flows, issues
    if not isinstance(rows, list):
        raise ValidationError("回放流量必须是行数组")
    if len(rows) > MAX_ROWS:
        issues.append({"scope": "traffic", "message": f"流量最多 {MAX_ROWS} 行"})
        return flows, issues

    for idx, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            issues.append({"scope": "traffic", "row": idx, "message": "行格式错误"})
            continue
        raw_source = _row_get(row, "source", "src", "s")
        raw_group = _row_get(row, "group", "g", "grp")
        raw_frames = _row_get(row, "frames", "frame_count", "count", "n")

        if _is_blank(raw_source) and _is_blank(raw_group) and _is_blank(raw_frames):
            continue

        source = group = None
        frames = None
        try:
            source = parse_ipv4(raw_source, "源地址")
        except ValueError as exc:
            issues.append({"scope": "traffic", "row": idx, "field": "source", "message": str(exc)})
        try:
            group = parse_ipv4(raw_group, "组地址")
            if not is_multicast(group):
                issues.append({
                    "scope": "traffic", "row": idx, "field": "group",
                    "message": f"组地址 {group} 不在 IPv4 组播段 224.0.0.0/4",
                })
        except ValueError as exc:
            issues.append({"scope": "traffic", "row": idx, "field": "group", "message": str(exc)})
        try:
            frames = parse_nonneg_int(raw_frames, "帧数")
            if frames == 0 and not _is_blank(raw_frames):
                # zero is accepted but pointless; keep it for exact replay
                pass
        except ValueError as exc:
            issues.append({"scope": "traffic", "row": idx, "field": "frames", "message": str(exc)})

        if source and group and frames is not None:
            flows.append({"source": source, "group": group, "frames": frames})

    flows.sort(key=lambda f: (int(ipaddress.IPv4Address(f["group"])),
                              int(ipaddress.IPv4Address(f["source"]))))
    return flows, issues


def normalize_limits(raw) -> tuple[dict | None, list[dict]]:
    issues: list[dict] = []
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        return None, [{"scope": "limits", "message": "上限必须是对象"}]
    hw_raw = _row_get(raw, "hw", "hw_limit", "hardware", "hardware_limit", "max_entries")
    cpu_raw = _row_get(raw, "cpu", "cpu_limit", "max_cpu_frames", "cpu_frames")
    hw = cpu = None
    try:
        hw = parse_nonneg_int(hw_raw, "硬件表项上限", default=10**9)
    except ValueError as exc:
        issues.append({"scope": "limits", "field": "hw", "message": str(exc)})
    try:
        cpu = parse_nonneg_int(cpu_raw, "CPU 帧上限", default=10**18)
    except ValueError as exc:
        issues.append({"scope": "limits", "field": "cpu", "message": str(exc)})
    if issues:
        return None, issues
    return {"hw": hw, "cpu": cpu}, issues


def normalize_rules(rows) -> tuple[list[dict], list[dict]]:
    """Validate a candidate hardware rule set (used by /api/evaluate and
    optional manual candidates)."""
    issues: list[dict] = []
    rules: list[dict] = []
    if rows is None:
        return rules, issues
    if not isinstance(rows, list):
        raise ValidationError("硬件规则必须是数组")

    seen: set[tuple] = set()
    for idx, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            issues.append({"scope": "rules", "row": idx, "message": "规则格式错误"})
            continue
        raw_source = _row_get(row, "source", "src")
        raw_group = _row_get(row, "group", "g")
        raw_action = (_row_get(row, "action", "to") or "ports")
        raw_ports = _row_get(row, "ports", "egress_ports", "out_ports")
        try:
            source = parse_source(raw_source)
        except ValueError as exc:
            issues.append({"scope": "rules", "row": idx, "field": "source", "message": str(exc)})
            continue
        try:
            group = parse_ipv4(raw_group, "组地址")
        except ValueError as exc:
            issues.append({"scope": "rules", "row": idx, "field": "group", "message": str(exc)})
            continue

        key = (source, group)
        if key in seen:
            kind = f"({source}, {group})" if source else f"(*, {group})"
            issues.append({"scope": "rules", "row": idx, "message": f"硬件表项 {kind} 重复"})
            continue
        seen.add(key)

        action = str(raw_action).strip().lower()
        if action in ("cpu", "to_cpu", "punt"):
            rules.append({"source": source, "group": group, "action": "cpu", "ports": []})
            continue
        if action not in ("ports", "port", "egress", "drop"):
            issues.append({"scope": "rules", "row": idx, "field": "action",
                           "message": f"未知动作 {raw_action!r}（应为 ports 或 cpu）"})
            continue
        try:
            ports = parse_ports(raw_ports if raw_ports is not None else [])
        except ValueError as exc:
            issues.append({"scope": "rules", "row": idx, "field": "ports", "message": str(exc)})
            continue
        if action == "drop":
            ports = []
        if len(ports) > 2:
            issues.append({"scope": "rules", "row": idx, "field": "ports",
                           "message": f"一条硬件表项最多指定 2 个出端口，当前 {len(ports)} 个: {ports}"})
            continue
        rules.append({"source": source, "group": group, "action": "ports", "ports": ports})

    return rules, issues


def normalize_drill_payload(payload: dict) -> dict:
    """Normalize a full drill payload.

    Raises ValidationError with all row-level issues collected.
    """
    if not isinstance(payload, dict):
        raise ValidationError("请求体必须是 JSON 对象")

    listening, l_issues = normalize_listening(
        _row_get(payload, "listening", "snooping", "entries"))
    traffic, t_issues = normalize_traffic(
        _row_get(payload, "traffic", "flows", "replay"))
    limits, lim_issues = normalize_limits(
        _row_get(payload, "limits") if _row_get(payload, "limits") is not None
        else {"hw": payload.get("hw_limit"), "cpu": payload.get("cpu_limit")})

    rules = None
    r_issues: list[dict] = []
    if _row_get(payload, "rules", "candidate_rules") is not None:
        rules, r_issues = normalize_rules(_row_get(payload, "rules", "candidate_rules"))

    issues = l_issues + t_issues + lim_issues + r_issues
    if issues:
        err = ValidationError("输入校验失败")
        err.issues = issues
        raise err

    return {
        "listening": listening,
        "traffic": traffic,
        "limits": limits,
        "rules": rules,
        "simulate_fail": bool(payload.get("simulate_fail", False)
                             or payload.get("simulate_publish_failure", False)),
    }


def _is_blank(value) -> bool:
    if value is None:
        return True
    if isinstance(value, (list, tuple, set)) and len(value) == 0:
        return True
    return isinstance(value, str) and value.strip() == ""
