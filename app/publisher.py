"""Simulated push of a synthesized configuration to the switch.

The real project would call a device agent / gNMI / NETCONF endpoint here.
For the local drill page the push is an in-process action with a small delay
(so the "version switch" window is observable) and two failure injections:

* explicit per-request ``simulate_fail`` checkbox / JSON flag;
* optional random rate via ``PUBLISH_FAIL_RATE`` (0 by default).
"""
from __future__ import annotations

import os
import random
import time
import uuid


class PublishError(RuntimeError):
    pass


class Publisher:
    def __init__(self, delay: float | None = None,
                 fail_rate: float | None = None):
        self.delay = float(os.environ.get("PUBLISH_DELAY", "0.6")) if delay is None else delay
        self.fail_rate = float(os.environ.get("PUBLISH_FAIL_RATE", "0")) if fail_rate is None else fail_rate

    def push(self, snapshot: dict, *, simulate_fail: bool = False) -> dict:
        # Simulate the slow device transaction / agent round-trip.
        if self.delay > 0:
            time.sleep(self.delay)

        if simulate_fail:
            raise PublishError(
                "模拟发布失败：交换机 agent 拒绝整份候选配置（未提交任何表项）")
        if self.fail_rate > 0 and random.random() < self.fail_rate:
            raise PublishError("模拟发布失败：设备事务超时（整份配置未生效）")

        return {
            "transaction": uuid.uuid4().hex[:12],
            "entry_count": len(snapshot.get("rules", [])),
            "pushed_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        }
