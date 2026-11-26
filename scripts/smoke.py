#!/usr/bin/env python3
"""End-to-end smoke check for the multicast drill console.

Stdlib only.  Usage:

    python3 scripts/smoke.py [BASE_URL]

It exercises the headline three-destination-port frame, the replay CPU total,
infeasible / failed-publish rollback behaviour and the mixed S,G / *,G solve.
The script mutates the target instance by publishing example revisions.
"""
import json
import sys
import urllib.error
import urllib.request

BASE = sys.argv[1].rstrip("/") if len(sys.argv) > 1 else "http://127.0.0.1:8000"

THREE_PORTS = {
    "listening": [{"source": None, "group": "239.20.0.1",
                   "ports": ["1", "2", "3"]}],
    "traffic": [{"source": "172.16.0.10", "group": "239.20.0.1", "frames": 120}],
    "limits": {"hw": 4, "cpu": 120},
}

IMPOSSIBLE = {
    "listening": [{"source": None, "group": "239.9.9.9",
                   "ports": ["1", "2", "3", "4"]}],
    "traffic": [{"source": "10.9.9.9", "group": "239.9.9.9", "frames": 5000}],
    "limits": {"hw": 10, "cpu": 100},
}

MIXED = {
    "listening": [
        {"source": None, "group": "239.1.1.1", "ports": ["1", "2"]},
        {"source": "10.1.1.1", "group": "239.1.1.1", "ports": ["3", "4"]},
        {"source": "10.1.1.2", "group": "239.1.1.1", "ports": ["1", "2", "5"]},
        {"source": None, "group": "239.1.1.2", "ports": ["5", "6"]},
    ],
    "traffic": [
        {"source": "10.1.1.99", "group": "239.1.1.1", "frames": 5000},
        {"source": "10.1.1.98", "group": "239.1.1.1", "frames": 3000},
        {"source": "10.1.1.97", "group": "239.1.1.1", "frames": 2000},
        {"source": "10.1.1.1", "group": "239.1.1.1", "frames": 1000},
        {"source": "10.1.1.2", "group": "239.1.1.1", "frames": 700},
        {"source": "10.1.1.9", "group": "239.1.1.2", "frames": 4000},
    ],
    "limits": {"hw": 4, "cpu": 700},
}

failures = []


def call(method, path, body=None, base=BASE):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        base + path, data=data, method=method,
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())


def check(name, cond, detail=""):
    mark = "PASS" if cond else "FAIL"
    print(f"[{mark}] {name}" + (f"  -- {detail}" if detail else ""))
    if not cond:
        failures.append(name)


def main():
    print(f"== smoke against {BASE} ==")
    st, health = call("GET", "/healthz")
    check("healthz 200", st == 200 and health.get("status") == "ok", str(health))
    before = health.get("active_revision_id")

    # 1) Stateless evaluation of a frame that must reach three ports.
    st, ev = call("POST", "/api/evaluate", {
        "listening": THREE_PORTS["listening"],
        "traffic": THREE_PORTS["traffic"],
        "limits": {"hw": 0, "cpu": 10**9},
        "rules": [],
    })
    row = ev["evaluation"]["rows"][0]
    check("三端口帧: 无硬件表项 -> CPU", st == 200 and row["via"] == "cpu")
    check("三端口帧: 实际端口恰为 [1,2,3]",
          row["actual_ports"] == ["1", "2", "3"] and row["correct"],
          str(row["actual_ports"]))
    check("三端口回放 CPU 帧=120", ev["evaluation"]["cpu_frames"] == 120)

    # 2) Infeasible drill must not change active config.
    st, body = call("POST", "/api/drills", IMPOSSIBLE)
    check("无解提交返回 409/no_solution",
          st == 409 and body.get("code") == "no_solution")
    _, h2 = call("GET", "/healthz")
    check("无解后仍使用原已发布配置", h2["active_revision_id"] == before,
          f"before={before} after={h2['active_revision_id']}")

    # 3) Simulated publish failure must not change active config.
    pf = dict(THREE_PORTS)
    pf["simulate_fail"] = True
    st, body = call("POST", "/api/drills", pf)
    check("模拟发布失败返回 502/publish_failed",
          st == 502 and body.get("code") == "publish_failed")
    _, h3 = call("GET", "/healthz")
    check("发布失败后仍使用 SQLite 已发布配置",
          h3["active_revision_id"] == before)

    # 4) Feasible three-port drill publishes; trial send arbitrary source.
    st, pub = call("POST", "/api/drills", THREE_PORTS)
    check("三端口配法可发布 (201)", st == 201 and pub.get("ok"),
          f"revision=#{pub.get('revision_id')}")
    active_id = pub["revision_id"]

    st, sent = call("POST", "/api/send", {
        "source": "198.51.100.66", "group": "239.20.0.1",
        "revision_id": active_id})
    f = sent["frame"]
    check("任意未列出源试发 -> CPU + 三端口完全一致",
          st == 200 and f["via"] == "cpu" and f["correct"]
          and f["actual_ports"] == ["1", "2", "3"],
          f"via={f['via']} actual={f['actual_ports']}")

    st, rep = call("POST", "/api/replay", {"revision_id": active_id})
    check("该版回放 CPU 帧总数=120 且全部正确",
          rep["cpu_frames"] == 120 and rep["all_correct"],
          f"cpu={rep['cpu_frames']} total={rep['total_frames']}")

    # 5) Mixed S,G / *,G optimum.
    st, mix = call("POST", "/api/drills", MIXED)
    snap = mix.get("snapshot", {})
    check("混合场景发布 4 表项 / 700 CPU 帧",
          st == 201 and snap.get("solver", {}).get("entries") == 4
          and snap.get("solver", {}).get("cpu_frames") == 700)
    rules = {(r["source"], r["group"]): r for r in snap.get("rules", [])}
    check("(*,239.1.1.1) 聚合到 1,2",
          (None, "239.1.1.1") in rules
          and rules[(None, "239.1.1.1")]["ports"] == ["1", "2"])
    check("(10.1.1.2,239.1.1.1) 为重端口例外 -> cpu 动作",
          rules.get(("10.1.1.2", "239.1.1.1"), {}).get("action") == "cpu")

    print()
    if failures:
        print(f"SMOKE FAILED: {len(failures)} check(s): {failures}")
        sys.exit(1)
    print("SMOKE OK: all checks passed")


if __name__ == "__main__":
    main()
