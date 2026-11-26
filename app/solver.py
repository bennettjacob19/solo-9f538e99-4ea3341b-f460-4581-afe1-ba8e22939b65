"""Configuration synthesis.

Problem
-------
Given a listening table, weighted replay flows and two caps

    H = maximum hardware entries
    C = maximum CPU-forwarded replay frames

find a correct hardware rule set with ``len(rules) <= H`` and
``cpu_frames <= C``.  The hardware table is "almost full", so among feasible
configurations we minimise the number of hardware entries first (and then the
CPU frame count).

Per-group structure
-------------------
For one group G let D be the port set of its ``(*, G)`` listening entry
(``D = ∅`` when there is none) and ``R(S)`` the required port set for source
S (exact ``(S,G)`` listening entry if present, otherwise D).

Two families cover every useful correct configuration:

1. **specific-only (S)**: no ``(*,G)`` hardware rule.  Any source with
   ``|R(S)| <= 2`` may be offloaded by one ``(S,G)`` port rule; every other
   source misses to the CPU, which is always correct.  With k entries the best
   CPU saving comes from the k heaviest replay sources, giving one Pareto
   option per k.

2. **group aggregate (H)**: install ``(*,G) -> D`` (only encodable when
   ``|D| <= 2``).  Every exact listening source with ``R(S) != D`` must get a
   more-specific ``(S,G)`` rule, otherwise it would hit the aggregate with the
   wrong port set: a port rule when ``|R(S)| <= 2``, a CPU-punt rule
   otherwise.  The option has a fixed entry cost and a fixed CPU cost (replay
   weight of the ``|R(S)| > 2`` exceptions).

Per-group options are pruned to the Pareto frontier of ``(entries, cpu)`` and
combined across groups with a dynamic program keyed by total hardware
entries.  Every produced rule set is correct for *all* IPv4 sources by
construction; ``dataplane.audit_all_sources`` is used as an executable
cross-check.
"""
from __future__ import annotations

import ipaddress
from dataclasses import dataclass, field

from .dataplane import audit_all_sources, simulate_replay


@dataclass
class GroupOption:
    entries: int
    cpu: int
    rules: list[dict] = field(default_factory=list)
    kind: str = "specific"          # "specific" | "aggregate"
    description: str = ""
    offloaded_sources: list[str] = field(default_factory=list)


def _required_map(listening: list[dict], group: str):
    """Return D and dict source -> required ports (set) for exact entries."""
    d_ports: frozenset[str] = frozenset()
    exact: dict[str, frozenset[str]] = {}
    for e in listening:
        if e["group"] != group:
            continue
        ports = frozenset(e["ports"])
        if e["source"] is None:
            d_ports = ports
        else:
            exact[e["source"]] = ports

    def required(src: str) -> frozenset[str]:
        return exact.get(src, d_ports)

    return d_ports, exact, required


def _group_options(group: str, listening: list[dict],
                   weights: dict[str, int]) -> list[GroupOption]:
    d_ports, exact, required = _required_map(listening, group)
    total_weight = sum(weights.values())

    options: list[GroupOption] = []

    # ---- Family S: specific-only, top-k heaviest offload candidates ----
    candidates = []
    for src, w in weights.items():
        req = required(src)
        if len(req) <= 2 and w > 0:
            candidates.append((w, src, req))
    # Deterministic: heavier first, ties by IP string.
    candidates.sort(key=lambda t: (-t[0], t[1]))

    prefix_rules: list[dict] = []
    saved = 0
    # k = 0: offload nothing.
    options.append(GroupOption(
        entries=0, cpu=total_weight, rules=[], kind="specific",
        description="不装硬件表项，全部交 CPU"))
    for w, src, req in candidates:
        prefix_rules.append({
            "source": src, "group": group, "action": "ports",
            "ports": sorted(req),
        })
        saved += w
        options.append(GroupOption(
            entries=len(prefix_rules), cpu=total_weight - saved,
            rules=[dict(r) for r in prefix_rules], kind="specific",
            offloaded_sources=[r["source"] for r in prefix_rules],
            description=f"按源 offload 最重的 {len(prefix_rules)} 条流"))

    # ---- Family H: (*,G) aggregate plus mandatory exceptions ----
    if len(d_ports) <= 2:
        rules: list[dict] = [{
            "source": None, "group": group, "action": "ports",
            "ports": sorted(d_ports),
        }]
        cpu_cost = 0
        heavy: list[str] = []
        for src in sorted(exact.keys()):
            req = exact[src]
            if req == d_ports:
                continue
            if len(req) <= 2:
                rules.append({
                    "source": src, "group": group, "action": "ports",
                    "ports": sorted(req),
                })
            else:
                rules.append({
                    "source": src, "group": group, "action": "cpu",
                    "ports": [],
                })
                cpu_cost += weights.get(src, 0)
                heavy.append(src)
        desc = f"(*,{group}) 聚合到端口 {sorted(d_ports) or '∅(硬件丢弃)'}"
        if heavy:
            desc += f"；{len(heavy)} 个 >2 端口源必须 punt CPU"
        options.append(GroupOption(
            entries=len(rules), cpu=cpu_cost, rules=rules,
            kind="aggregate", description=desc))

    return _pareto(options)


def _pareto(options: list[GroupOption]) -> list[GroupOption]:
    """Drop options dominated on (entries, cpu); keep one per trade-off."""
    # Best cpu for each exact entry count.
    best_by_entries: dict[int, GroupOption] = {}
    for opt in options:
        cur = best_by_entries.get(opt.entries)
        if cur is None or opt.cpu < cur.cpu or (
                opt.cpu == cur.cpu and _tie_prefer(opt, cur)):
            best_by_entries[opt.entries] = opt

    kept: list[GroupOption] = []
    for e in sorted(best_by_entries):
        opt = best_by_entries[e]
        dominated = any(
            other is not opt
            and other.entries <= opt.entries and other.cpu <= opt.cpu
            and (other.entries < opt.entries or other.cpu < opt.cpu)
            for other in best_by_entries.values()
        )
        if not dominated:
            kept.append(opt)
    return kept


def _tie_prefer(a: GroupOption, b: GroupOption) -> bool:
    # On equal stats prefer the aggregate showcase (exercises (*,G)+(S,G)),
    # otherwise deterministic smaller JSON.
    if a.kind != b.kind:
        return a.kind == "aggregate"
    return len(str(a.rules)) < len(str(b.rules))


def solve(listening: list[dict], traffic: list[dict],
          limits: dict) -> dict:
    """Return a solver result dict.

    Success::

        {"feasible": True, "rules": [...], "entries": n,
         "cpu_frames": c, "hw_limit": H, "cpu_limit": C,
         "groups": [per-group explanations]}

    Failure::

        {"feasible": False, "reason": "...", "minimum_entries": ...,
         "minimum_cpu_at_entry_cap": ..., ...}
    """
    hw_limit = int(limits["hw"])
    cpu_limit = int(limits["cpu"])

    # Aggregate replay weight by (source, group).
    weights_by_group: dict[str, dict[str, int]] = {}
    for flow in traffic or []:
        weights_by_group.setdefault(flow["group"], {})
        wg = weights_by_group[flow["group"]]
        wg[flow["source"]] = wg.get(flow["source"], 0) + int(flow["frames"])

    groups = sorted(
        {e["group"] for e in listening} | set(weights_by_group.keys()),
        key=lambda g: int(ipaddress.IPv4Address(g)),
    )

    group_options: dict[str, list[GroupOption]] = {}
    for g in groups:
        group_options[g] = _group_options(
            g, listening, weights_by_group.get(g, {}))

    # DP: exact entry count e -> (cpu, choices[(group, GroupOption)])
    dp: dict[int, tuple[int, list]] = {0: (0, [])}
    for g in groups:
        new_dp: dict[int, tuple[int, list]] = {}
        for used, (cpu, choices) in dp.items():
            for opt in group_options[g]:
                ne = used + opt.entries
                nc = cpu + opt.cpu
                if ne > hw_limit or nc > cpu_limit:
                    continue
                cur = new_dp.get(ne)
                if cur is None or nc < cur[0]:
                    new_dp[ne] = (nc, choices + [(g, opt)])
        dp = new_dp
        if not dp:
            break

    if dp:
        # Entry-conserving: smallest entry count that satisfies the CPU cap.
        best_e = min(dp.keys(), key=lambda e: (e, dp[e][0]))
        best_cpu, choices = dp[best_e]
        rules: list[dict] = []
        group_report = []
        for g, opt in choices:
            rules.extend(opt.rules)
            group_report.append({
                "group": g,
                "kind": opt.kind,
                "entries": opt.entries,
                "cpu_frames": opt.cpu,
                "description": opt.description,
            })
        rules.sort(key=lambda r: (
            int(ipaddress.IPv4Address(r["group"])),
            r["source"] is not None, r["source"] or ""))

        replay = simulate_replay(listening, rules, traffic)
        audit = audit_all_sources(
            listening, rules,
            extra_sources=[f["source"] for f in traffic])
        assert replay["cpu_frames"] == best_cpu, (replay["cpu_frames"], best_cpu)
        assert replay["all_correct"] and audit["all_correct"]

        return {
            "feasible": True,
            "rules": rules,
            "entries": len(rules),
            "cpu_frames": replay["cpu_frames"],
            "total_frames": replay["total_frames"],
            "hardware_frames": replay["hardware_frames"],
            "hw_limit": hw_limit,
            "cpu_limit": cpu_limit,
            "groups": group_report,
            "audit": audit,
        }

    # Infeasible diagnostics: global minimum CPU ignoring the CPU cap, but
    # respecting the hardware cap; and the absolute minimum possible CPU.
    min_cpu_within_hw = _min_cpu(group_options, hw_limit)
    absolute_min_cpu = _min_cpu(group_options, 10**12)
    min_entries_for_cpu = _min_entries_for_cpu(group_options, cpu_limit)

    reason = _infeasible_reason(
        hw_limit, cpu_limit, min_cpu_within_hw, absolute_min_cpu,
        min_entries_for_cpu)
    return {
        "feasible": False,
        "reason": reason,
        "hw_limit": hw_limit,
        "cpu_limit": cpu_limit,
        "min_cpu_within_hw": min_cpu_within_hw,
        "absolute_min_cpu": absolute_min_cpu,
        "min_entries_for_cpu_cap": min_entries_for_cpu,
        "group_options": {
            g: [{"entries": o.entries, "cpu": o.cpu, "kind": o.kind}
                for o in opts]
            for g, opts in group_options.items()
        },
    }


def _min_cpu(group_options, entry_cap: int):
    dp = {0: 0}
    for _g, opts in group_options.items():
        ndp: dict[int, int] = {}
        for e, cpu in dp.items():
            for o in opts:
                ne = e + o.entries
                if ne > entry_cap:
                    continue
                nc = cpu + o.cpu
                if ne not in ndp or nc < ndp[ne]:
                    ndp[ne] = nc
        dp = ndp
    return min(dp.values()) if dp else None


def _min_entries_for_cpu(group_options, cpu_cap: int):
    dp = {0: 0}  # entries -> cpu
    for _g, opts in group_options.items():
        ndp: dict[int, int] = {}
        for e, cpu in dp.items():
            for o in opts:
                ne = e + o.entries
                nc = cpu + o.cpu
                if nc > cpu_cap:
                    continue
                if ne not in ndp or nc < ndp[ne]:
                    ndp[ne] = nc
        dp = ndp
    return min(dp.keys()) if dp else None


def _infeasible_reason(hw, cpu, min_cpu_hw, abs_min_cpu, min_entries_cpu):
    parts = ["不存在同时满足两项上限的配置："]
    if abs_min_cpu is not None and abs_min_cpu > cpu:
        parts.append(
            f"即使硬件表项不限量，最少仍有 {abs_min_cpu} 帧必须由 CPU 转发"
            f"（接收端口超过 2 个的组无法编码进单条硬件表项），"
            f"高于 CPU 上限 {cpu}。")
    elif min_cpu_hw is not None and min_cpu_hw > cpu:
        parts.append(
            f"在硬件表项上限 {hw} 条以内，回放仍有至少 {min_cpu_hw} CPU 帧，"
            f"高于 CPU 上限 {cpu}；满足 CPU 上限至少需要 "
            f"{min_entries_cpu} 条硬件表项。")
    if min_entries_cpu is not None and min_entries_cpu > hw:
        parts.append(
            f"把 CPU 帧压到 {cpu} 以内至少需要 {min_entries_cpu} 条表项，"
            f"超过硬件上限 {hw}。")
    if len(parts) == 1:
        parts.append("两项上限的组合下 DP 未找到可行解。")
    return " ".join(parts)
