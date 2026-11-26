"""Application service: drill submission, publish transaction, replay.

Concurrency model
-----------------
* Publish attempts are serialized by an in-process lock; each successful
  publish is one SQLite row in one transaction.
* A replay never takes the publish lock.  It reads the latest published
  revision **once** at start and simulates the whole round from that complete
  JSON snapshot, so a version switch mid-round cannot mix old/new rules.
"""
from __future__ import annotations

import threading

from .dataplane import audit_all_sources, simulate_replay
from .model import normalize_drill_payload
from .publisher import PublishError, Publisher
from .solver import solve
from .storage import Storage, utcnow_iso


class DrillService:
    def __init__(self, storage: Storage, publisher: Publisher | None = None):
        self.storage = storage
        self.publisher = publisher or Publisher()
        self.publish_lock = threading.Lock()

    # ---------------- active config ----------------

    def active_revision(self) -> dict | None:
        return self.storage.latest_published()

    def active_snapshot(self) -> dict | None:
        rev = self.storage.latest_published()
        return rev["payload"] if rev else None

    def snapshot_for(self, revision_id: int | None) -> tuple[dict, dict | None]:
        """Return (snapshot, revision_row) for an id or the active one."""
        if revision_id is None:
            rev = self.storage.latest_published()
        else:
            rev = self.storage.get_revision(int(revision_id))
            if rev is None or rev["status"] != "published":
                return None, None
        if rev is None:
            return None, None
        return rev["payload"], rev

    # ---------------- drill + publish ----------------

    def submit_drill(self, raw_payload: dict) -> dict:
        # ValidationError propagates to the route layer (HTTP 400).
        normalized = normalize_drill_payload(raw_payload)
        listening = normalized["listening"]
        traffic = normalized["traffic"]
        limits = normalized["limits"]
        simulate_fail = normalized["simulate_fail"]

        with self.publish_lock:
            # Optional operator-supplied candidate rules (advanced API).
            if normalized["rules"] is not None:
                candidate = self._check_manual_rules(
                    listening, traffic, limits, normalized["rules"])
                if not candidate["valid"]:
                    rev_id = self.storage.insert_revision(
                        status="invalid_rules",
                        simulate_fail=simulate_fail,
                        message=candidate["message"],
                        entries=len(normalized["rules"]),
                        cpu_frames=candidate["evaluation"]["cpu_frames"],
                        hw_limit=limits["hw"], cpu_limit=limits["cpu"],
                        payload={"input": normalized,
                                 "evaluation": candidate["evaluation"]},
                    )
                    return {"ok": False, "code": "invalid_rules",
                            "revision_id": rev_id, **candidate}
                solution = {
                    "feasible": True,
                    "rules": normalized["rules"],
                    "entries": len(normalized["rules"]),
                    "cpu_frames": candidate["evaluation"]["cpu_frames"],
                    "total_frames": candidate["evaluation"]["total_frames"],
                    "hardware_frames": candidate["evaluation"]["hardware_frames"],
                    "groups": [],
                    "audit": candidate["audit"],
                    "manual": True,
                }
            else:
                solution = solve(listening, traffic, limits)

            if not solution["feasible"]:
                rev_id = self.storage.insert_revision(
                    status="no_solution",
                    simulate_fail=simulate_fail,
                    message=solution["reason"],
                    entries=None,
                    cpu_frames=solution.get("min_cpu_within_hw"),
                    hw_limit=limits["hw"], cpu_limit=limits["cpu"],
                    payload={"input": normalized, "solution": solution},
                )
                active = self.storage.latest_published()
                return {
                    "ok": False, "code": "no_solution",
                    "revision_id": rev_id,
                    "message": solution["reason"],
                    "solution": solution,
                    "active_revision_id": active["id"] if active else None,
                }

            snapshot = {
                "published_at": utcnow_iso(),
                "listening": listening,
                "traffic": traffic,
                "limits": limits,
                "rules": solution["rules"],
                "solver": {
                    "entries": solution["entries"],
                    "cpu_frames": solution["cpu_frames"],
                    "total_frames": solution.get("total_frames"),
                    "hardware_frames": solution.get("hardware_frames"),
                    "groups": solution.get("groups", []),
                    "manual": solution.get("manual", False),
                },
            }

            try:
                push_info = self.publisher.push(
                    snapshot, simulate_fail=simulate_fail)
            except PublishError as exc:
                message = str(exc)
                rev_id = self.storage.insert_revision(
                    status="publish_failed",
                    simulate_fail=simulate_fail,
                    message=message,
                    entries=solution["entries"],
                    cpu_frames=solution["cpu_frames"],
                    hw_limit=limits["hw"], cpu_limit=limits["cpu"],
                    payload={"input": normalized,
                             "solution": solution,
                             "would_publish": snapshot},
                )
                active = self.storage.latest_published()
                return {
                    "ok": False, "code": "publish_failed",
                    "revision_id": rev_id,
                    "message": message,
                    "solution": solution,
                    "active_revision_id": active["id"] if active else None,
                }

            version_no = self.storage.next_version_no()
            snapshot["version"] = version_no
            snapshot["publish"] = push_info
            rev_id = self.storage.insert_revision(
                status="published", version_no=version_no,
                message=f"已发布版本 v{version_no}",
                entries=solution["entries"],
                cpu_frames=solution["cpu_frames"],
                hw_limit=limits["hw"], cpu_limit=limits["cpu"],
                simulate_fail=simulate_fail,
                payload=snapshot,
            )
            return {
                "ok": True, "code": "published",
                "revision_id": rev_id, "version": version_no,
                "snapshot": snapshot,
            }

    @staticmethod
    def _check_manual_rules(listening, traffic, limits, rules) -> dict:
        evaluation = simulate_replay(listening, rules, traffic)
        audit = audit_all_sources(
            listening, rules, extra_sources=[f["source"] for f in traffic])
        problems = []
        if len(rules) > limits["hw"]:
            problems.append(
                f"硬件表项 {len(rules)} 条超过上限 {limits['hw']}")
        if evaluation["cpu_frames"] > limits["cpu"]:
            problems.append(
                f"回放 CPU 帧 {evaluation['cpu_frames']} 超过上限 "
                f"{limits['cpu']}")
        if not evaluation["all_correct"]:
            bad = [r for r in evaluation["rows"] if not r["correct"]]
            problems.append(
                f"{len(bad)} 条回放流的实际端口与监听端口不完全一致")
        if not audit["all_correct"]:
            problems.append(
                f"全源审计发现 {audit['problem_count']} 个错误探针")
        return {
            "valid": not problems,
            "message": "；".join(problems) if problems else "候选规则校验通过",
            "evaluation": evaluation,
            "audit": audit,
        }

    # ---------------- replay / trial ----------------

    def run_replay(self, revision_id: int | None = None,
                   override_flows: list[dict] | None = None) -> dict:
        snapshot, rev = self.snapshot_for(revision_id)
        if snapshot is None:
            return {"ok": False, "code": "no_published_config",
                    "message": "SQLite 中还没有已发布配置"}

        # Whole-config snapshot fixed at round start.
        flows = override_flows if override_flows is not None else snapshot.get("traffic", [])
        result = simulate_replay(
            snapshot.get("listening", []), snapshot.get("rules", []), flows)
        run_id = self.storage.insert_replay_run(
            revision_id=rev["id"], detail={
                "revision_id": rev["id"],
                "version": snapshot.get("version"),
                "flows": flows,
                "result": result,
            },
            total_frames=result["total_frames"],
            cpu_frames=result["cpu_frames"],
            hw_frames=result["hardware_frames"],
            all_correct=result["all_correct"],
        )
        return {
            "ok": True, "run_id": run_id,
            "revision_id": rev["id"],
            "version": snapshot.get("version"),
            "hw_limit": snapshot.get("limits", {}).get("hw"),
            "cpu_limit": snapshot.get("limits", {}).get("cpu"),
            "entries": len(snapshot.get("rules", [])),
            **result,
        }

    def trial_send(self, source: str, group: str,
                   revision_id: int | None = None) -> dict:
        snapshot, rev = self.snapshot_for(revision_id)
        if snapshot is None:
            return {"ok": False, "code": "no_published_config",
                    "message": "SQLite 中还没有已发布配置"}
        from .dataplane import make_indexes, forward_frame
        l_idx, h_idx = make_indexes(
            snapshot.get("listening", []), snapshot.get("rules", []))
        frame = forward_frame(l_idx, h_idx, source, group)
        return {"ok": True, "revision_id": rev["id"],
                "version": snapshot.get("version"), "frame": frame}

    def evaluate_payload(self, payload: dict) -> dict:
        """Stateless evaluation of arbitrary listening/rules/flows."""
        normalized = normalize_drill_payload(payload)
        rules = normalized["rules"]
        if rules is None:
            solution = solve(
                normalized["listening"], normalized["traffic"],
                normalized["limits"])
            if not solution["feasible"]:
                return {"ok": False, "solution": solution}
            rules = solution["rules"]
        evaluation = simulate_replay(
            normalized["listening"], rules, normalized["traffic"])
        audit = audit_all_sources(
            normalized["listening"], rules,
            extra_sources=[f["source"] for f in normalized["traffic"]])
        within = {
            "hw": len(rules) <= normalized["limits"]["hw"],
            "cpu": evaluation["cpu_frames"] <= normalized["limits"]["cpu"],
            "correct": evaluation["all_correct"] and audit["all_correct"],
        }
        return {
            "ok": all(within.values()),
            "rules": rules,
            "evaluation": evaluation,
            "audit": audit,
            "limits": normalized["limits"],
            "within": within,
        }
