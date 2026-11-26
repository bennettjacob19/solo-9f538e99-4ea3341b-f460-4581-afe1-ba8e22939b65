import json
import os
import shutil
import tempfile
import unittest

from app import create_app
from app.examples import IMPOSSIBLE, THREE_PORTS


class ApiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="drill-test-")
        self.db = os.path.join(self.tmp, "sub", "test.db")
        self.app = create_app({
            "TESTING": True,
            "DB_PATH": self.db,
            "PUBLISH_DELAY": 0,
            "PUBLISH_FAIL_RATE": 0,
        })
        self.client = self.app.test_client()

    def tearDown(self):
        self.app.extensions["drill_storage"].close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---- seed / health ----

    def test_health_and_seed_config(self):
        r = self.client.get("/healthz")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["status"], "ok")
        self.assertEqual(r.get_json()["active_revision_id"], 1)

        r = self.client.get("/api/config")
        body = r.get_json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["snapshot"]["version"], 1)
        self.assertEqual(len(body["snapshot"]["rules"]), 2)

    # ---- trial send: the three-port frame ----

    def test_trial_three_port_frame_goes_cpu_with_exact_ports(self):
        r = self.client.post("/api/send", json={
            "source": "198.51.100.77",       # arbitrary, unlisted source
            "group": "239.10.2.2",
        })
        self.assertEqual(r.status_code, 200, r.data)
        frame = r.get_json()["frame"]
        self.assertEqual(frame["via"], "cpu")
        self.assertEqual(frame["cpu_reason"], "miss")
        self.assertEqual(frame["actual_ports"], ["1", "2", "3"])
        self.assertEqual(frame["required_ports"], ["1", "2", "3"])
        self.assertTrue(frame["correct"])

    def test_trial_arbitrary_unknown_group_drops_cleanly(self):
        r = self.client.post("/api/send", json={
            "source": "8.8.8.8", "group": "239.250.250.250"
        })
        # 239.250... is valid multicast? 239.x yes.
        frame = r.get_json()["frame"]
        self.assertTrue(frame["correct"])
        self.assertEqual(frame["actual_ports"], [])
        self.assertEqual(frame["via"], "cpu")

    def test_send_rejects_bad_ip(self):
        r = self.client.post("/api/send", json={
            "source": "not-ip", "group": "239.1.1.1"})
        self.assertEqual(r.status_code, 400)
        self.assertFalse(r.get_json()["ok"])

    # ---- replay ----

    def test_replay_seed_cpu_total_300(self):
        r = self.client.post("/api/replay", json={})
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["cpu_frames"], 300)
        self.assertEqual(body["total_frames"], 1000)
        self.assertTrue(body["all_correct"])
        self.assertIsNotNone(body["run_id"])

    # ---- submit: success / no solution / publish failure ----

    def test_submit_publishes_feasible_drill(self):
        r = self.client.post("/api/drills", json=THREE_PORTS)
        self.assertEqual(r.status_code, 201, r.data)
        body = r.get_json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["version"], 2)
        self.assertEqual(body["snapshot"]["solver"]["entries"], 0)

        active = self.client.get("/api/config").get_json()["snapshot"]
        self.assertEqual(active["version"], 2)

    def test_no_solution_keeps_previous_published_config(self):
        r = self.client.post("/api/drills", json=IMPOSSIBLE)
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.get_json()["code"], "no_solution")

        cfg = self.client.get("/api/config").get_json()["snapshot"]
        self.assertEqual(cfg["version"], 1)  # unchanged SQLite config

        revs = self.client.get("/api/revisions").get_json()
        statuses = [x["status"] for x in revs]
        self.assertIn("no_solution", statuses)
        self.assertEqual(statuses.count("published"), 1)

    def test_simulated_publish_failure_keeps_old_config(self):
        payload = dict(THREE_PORTS)
        payload["simulate_fail"] = True
        r = self.client.post("/api/drills", json=payload)
        self.assertEqual(r.status_code, 502)
        body = r.get_json()
        self.assertEqual(body["code"], "publish_failed")
        self.assertIn("模拟发布失败", body["message"])

        cfg = self.client.get("/api/config").get_json()["snapshot"]
        self.assertEqual(cfg["version"], 1)
        self.assertEqual(body["active_revision_id"], 1)

        revs = self.client.get("/api/revisions").get_json()
        self.assertIn("publish_failed", [x["status"] for x in revs])

    def test_invalid_input_returns_issues(self):
        r = self.client.post("/api/drills", json={
            "listening": [{"source": "???", "group": "239.1.1.1",
                           "ports": "1,2,3"}],
            "traffic": [],
            "limits": {"hw": 1, "cpu": 1},
        })
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.get_json()["code"], "invalid_input")
        self.assertTrue(r.get_json()["error"]["issues"])

    # ---- evaluate arbitrary candidate rules ----

    def test_evaluate_detects_wrong_manual_rules(self):
        r = self.client.post("/api/evaluate", json={
            "listening": [
                {"source": None, "group": "239.4.4.4", "ports": ["1", "2"]}],
            "traffic": [
                {"source": "10.0.0.1", "group": "239.4.4.4", "frames": 3}],
            "limits": {"hw": 1, "cpu": 0},
            "rules": [
                {"source": None, "group": "239.4.4.4",
                 "action": "ports", "ports": ["9"]}],
        })
        body = r.get_json()
        self.assertFalse(body["ok"])
        self.assertFalse(body["within"]["correct"])

    def test_evaluate_rejects_rule_with_more_than_two_ports(self):
        r = self.client.post("/api/evaluate", json={
            "listening": [
                {"source": None, "group": "239.4.4.4",
                 "ports": ["1", "2", "3"]}],
            "traffic": [],
            "limits": {"hw": 1, "cpu": 1},
            "rules": [
                {"source": None, "group": "239.4.4.4",
                 "action": "ports", "ports": ["1", "2", "3"]}],
        })
        self.assertEqual(r.status_code, 400)

    # ---- snapshot consistency across a version switch ----

    def test_replay_snapshot_stays_whole_during_switch(self):
        # Read the whole active snapshot exactly as a replay round would.
        snap1 = self.client.get("/api/config").get_json()["snapshot"]
        self.assertEqual(snap1["version"], 1)

        # Publish a new version meanwhile.
        self.client.post("/api/drills", json=THREE_PORTS)
        snap2 = self.client.get("/api/config").get_json()["snapshot"]
        self.assertEqual(snap2["version"], 2)

        # The old round simulated from snap1 still yields the old CPU total.
        from app.dataplane import simulate_replay
        old = simulate_replay(snap1["listening"], snap1["rules"],
                              snap1["traffic"])
        new = simulate_replay(snap2["listening"], snap2["rules"],
                              snap2["traffic"])
        self.assertEqual(old["cpu_frames"], 300)
        self.assertEqual(new["cpu_frames"], 120)


if __name__ == "__main__":
    unittest.main()
