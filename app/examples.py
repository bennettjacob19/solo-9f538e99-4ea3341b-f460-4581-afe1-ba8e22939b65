"""Preset scenarios used by the landing page buttons and the initial seed."""
from __future__ import annotations

SEED = {
    "name": "已发布基线：含三端口组",
    "listening": [
        {"source": None, "group": "239.10.1.1", "ports": ["1", "2"]},
        {"source": "10.10.0.11", "group": "239.10.1.1", "ports": ["3", "4"]},
        {"source": None, "group": "239.10.2.2", "ports": ["1", "2", "3"]},
    ],
    "traffic": [
        {"source": "10.10.0.99", "group": "239.10.1.1", "frames": 500},
        {"source": "10.10.0.77", "group": "239.10.1.1", "frames": 80},
        {"source": "10.10.0.11", "group": "239.10.1.1", "frames": 120},
        {"source": "10.10.0.21", "group": "239.10.2.2", "frames": 300},
    ],
    "limits": {"hw": 8, "cpu": 300},
    "simulate_fail": False,
}

THREE_PORTS = {
    "name": "试跑：需发往三个端口的帧",
    "listening": [
        {"source": None, "group": "239.20.0.1", "ports": ["1", "2", "3"]},
    ],
    "traffic": [
        {"source": "172.16.0.10", "group": "239.20.0.1", "frames": 120},
    ],
    # 120 CPU frames <= 120 cap, 0 hardware entries -> publishable.
    "limits": {"hw": 4, "cpu": 120},
    "simulate_fail": False,
}

MIXED = {
    "name": "混合：按组 + 按源和组（后者优先）",
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
    # Aggregate strategy: (*,239.1.1.1) + 2 exceptions = 3 entries/700 CPU;
    # (*,239.1.1.2) = 1 entry/0 CPU -> exactly 4 entries and 700 CPU frames.
    "limits": {"hw": 4, "cpu": 700},
    "simulate_fail": False,
}

IMPOSSIBLE = {
    "name": "无解：三端口大流量撑爆 CPU 上限",
    "listening": [
        {"source": None, "group": "239.9.9.9", "ports": ["1", "2", "3", "4"]},
    ],
    "traffic": [
        {"source": "10.9.9.9", "group": "239.9.9.9", "frames": 5000},
    ],
    "limits": {"hw": 10, "cpu": 100},
    "simulate_fail": False,
}

PUBLISH_FAIL = {
    "name": "下发失败：配置可解，但模拟发布被设备拒绝",
    "listening": [
        {"source": None, "group": "239.8.8.8", "ports": ["7", "8"]},
    ],
    "traffic": [
        {"source": "10.8.8.8", "group": "239.8.8.8", "frames": 900},
    ],
    "limits": {"hw": 2, "cpu": 0},
    "simulate_fail": True,
}

EXAMPLES = {
    "seed": SEED,
    "three_ports": THREE_PORTS,
    "mixed": MIXED,
    "impossible": IMPOSSIBLE,
    "publish_fail": PUBLISH_FAIL,
}
