"""Multicast forwarding data-plane simulation.

Lookup order for both the hardware table and the snooping/listening table is
the same: exact ``(S, G)`` source-and-group match wins over ``(*, G)``
group-only match.

A hardware rule either forwards to an explicit port set (0..2 ports; an empty
set means hardware drop) or punts to the CPU.  A hardware miss also punts.
The CPU always forwards strictly according to the listening table.

Correctness criterion for one frame:

    actual_egress_ports == matched_listening_ports   (set equality)
"""
from __future__ import annotations

import ipaddress

from .netutils import sorted_ports

GENERIC_PROBE = "198.51.100.7"  # TEST-NET-2, used for the "any other source" audit


def _listening_index(listening: list[dict]):
    exact: dict[tuple[str, str], dict] = {}
    star: dict[str, dict] = {}
    for entry in listening or []:
        if entry.get("source"):
            exact[(entry["source"], entry["group"])] = entry
        else:
            star[entry["group"]] = entry
    return exact, star


def _hardware_index(rules: list[dict]):
    exact: dict[tuple[str, str], dict] = {}
    star: dict[str, dict] = {}
    for rule in rules or []:
        if rule.get("source"):
            exact[(rule["source"], rule["group"])] = rule
        else:
            star[rule["group"]] = rule
    return exact, star


def match_listening(index, source: str, group: str):
    exact, star = index
    entry = exact.get((source, group))
    if entry is not None:
        return entry, "S,G"
    entry = star.get(group)
    if entry is not None:
        return entry, "*,G"
    return None, None


def match_hardware(index, source: str, group: str):
    exact, star = index
    rule = exact.get((source, group))
    if rule is not None:
        return rule, "S,G"
    rule = star.get(group)
    if rule is not None:
        return rule, "*,G"
    return None, None


def forward_frame(listening_index, hw_index, source: str, group: str) -> dict:
    """Compute the forwarding outcome of one frame."""
    listen_entry, listen_match = match_listening(listening_index, source, group)
    required = set(listen_entry["ports"]) if listen_entry else set()

    hw_rule, hw_match = match_hardware(hw_index, source, group)

    result = {
        "source": source,
        "group": group,
        "required_ports": sorted_ports(required),
        "listening_match": listen_match,
        "actual_ports": [],
        "via": "cpu",                # "hardware" | "cpu"
        "cpu_reason": None,          # "hw_action" | "miss" | None
        "hw_match": hw_match,
        "hw_action": None,
        "correct": False,
    }

    if hw_rule is not None and hw_rule.get("action") == "ports":
        actual = set(hw_rule.get("ports") or [])
        result["via"] = "hardware"
        result["hw_action"] = "ports"
        result["actual_ports"] = sorted_ports(actual)
    else:
        if hw_rule is not None and hw_rule.get("action") == "cpu":
            result["cpu_reason"] = "hw_action"
            result["hw_action"] = "cpu"
        else:
            result["cpu_reason"] = "miss"
        # CPU forwards per listening table.
        result["actual_ports"] = sorted_ports(required)

    result["correct"] = set(result["actual_ports"]) == required
    return result


def make_indexes(listening: list[dict], rules: list[dict]):
    return _listening_index(listening), _hardware_index(rules)


def simulate_replay(listening: list[dict], rules: list[dict],
                    flows: list[dict]) -> dict:
    """Simulate every replayed flow.

    The forwarding decision is identical for every frame of one flow, so the
    CPU total is the frame-count weighted sum of punted flows.
    """
    l_idx, h_idx = make_indexes(listening, rules)
    rows: list[dict] = []
    total_frames = 0
    cpu_frames = 0
    hw_frames = 0
    all_correct = True

    for flow in flows or []:
        frame = forward_frame(l_idx, h_idx, flow["source"], flow["group"])
        n = int(flow.get("frames", 0))
        row = {**frame, "frames": n}
        row["cpu_frames"] = n if frame["via"] == "cpu" else 0
        total_frames += n
        cpu_frames += row["cpu_frames"]
        hw_frames += 0 if frame["via"] == "cpu" else n
        all_correct = all_correct and frame["correct"]
        rows.append(row)

    return {
        "rows": rows,
        "total_frames": total_frames,
        "cpu_frames": cpu_frames,
        "hardware_frames": hw_frames,
        "all_correct": all_correct,
    }


def audit_all_sources(listening: list[dict], rules: list[dict],
                      extra_sources: list[str] | None = None) -> dict:
    """Finite check of the "correct for *every* source" requirement.

    Forwarding behaviour can only differ for source addresses that appear as
    an exact listening entry or an exact hardware rule.  We therefore probe:
      * every exact listening source,
      * every exact hardware-rule source,
      * every caller-provided source (e.g. replay/trial sources),
      * one generic source guaranteed not to be any of the above, per group.

    If all probes are exact, the configuration is correct for all IPv4
    sources, because every unprobed source shares the generic path.
    """
    l_idx, h_idx = make_indexes(listening, rules)

    groups = sorted(
        {e["group"] for e in listening or []} | {r["group"] for r in rules or []},
        key=lambda g: int(ipaddress.IPv4Address(g)),
    )

    exact_listen_srcs = {s for (s, _g) in l_idx[0].keys()}
    exact_rule_srcs = {s for (s, _g) in h_idx[0].keys()}
    extra = set(extra_sources or [])

    probes: list[dict] = []
    problems: list[dict] = []

    for group in groups:
        sources = set(exact_listen_srcs) | set(exact_rule_srcs) | extra
        group_specific = {s for s in sources
                          if (s, group) in l_idx[0] or (s, group) in h_idx[0]
                          or s in extra}
        for src in sorted(group_specific, key=lambda s: int(ipaddress.IPv4Address(s))):
            res = forward_frame(l_idx, h_idx, src, group)
            res["probe_kind"] = "specific"
            probes.append(res)
            if not res["correct"]:
                problems.append(res)

        generic = _unused_source(sources)
        res = forward_frame(l_idx, h_idx, generic, group)
        res["probe_kind"] = "generic"
        probes.append(res)
        if not res["correct"]:
            problems.append(res)

    return {"probes": probes, "problem_count": len(problems),
            "all_correct": not problems}


def _unused_source(used: set[str]) -> str:
    addr = ipaddress.IPv4Address(GENERIC_PROBE)
    for _ in range(4096):
        candidate = str(addr)
        if candidate not in used:
            return candidate
        addr += 1
    # Extremely unlikely fallback.
    return "192.0.2.254"
