import unittest

from app.dataplane import audit_all_sources, simulate_replay
from app.examples import IMPOSSIBLE, MIXED, SEED, THREE_PORTS
from app.solver import solve


class SolverTest(unittest.TestCase):
    def test_three_port_group_cannot_be_offloaded(self):
        ex = THREE_PORTS
        res = solve(ex["listening"], ex["traffic"], ex["limits"])
        self.assertTrue(res["feasible"], res.get("reason"))
        # 120 CPU frames exactly meet the cap; zero entries is optimal.
        self.assertEqual(res["entries"], 0)
        self.assertEqual(res["cpu_frames"], 120)
        self.assertLessEqual(res["entries"], ex["limits"]["hw"])
        self.assertLessEqual(res["cpu_frames"], ex["limits"]["cpu"])

    def test_four_port_group_infeasible_under_small_cpu_cap(self):
        ex = IMPOSSIBLE
        res = solve(ex["listening"], ex["traffic"], ex["limits"])
        self.assertFalse(res["feasible"])
        self.assertEqual(res["absolute_min_cpu"], 5000)
        self.assertIn("CPU", res["reason"])

    def test_mixed_aggregate_strategy_and_priority(self):
        ex = MIXED
        res = solve(ex["listening"], ex["traffic"], ex["limits"])
        self.assertTrue(res["feasible"], res.get("reason"))
        self.assertEqual(res["entries"], 4)
        self.assertEqual(res["cpu_frames"], 700)
        kinds = {(r["source"], r["group"]): r for r in res["rules"]}

        star_g1 = kinds[(None, "239.1.1.1")]
        self.assertEqual(star_g1["action"], "ports")
        self.assertEqual(star_g1["ports"], ["1", "2"])

        sg_exception = kinds[("10.1.1.1", "239.1.1.1")]
        self.assertEqual(sg_exception["ports"], ["3", "4"])

        heavy = kinds[("10.1.1.2", "239.1.1.1")]
        self.assertEqual(heavy["action"], "cpu")

        star_g2 = kinds[(None, "239.1.1.2")]
        self.assertEqual(star_g2["ports"], ["5", "6"])

        # Executable cross-checks: replay + all-source audit both clean.
        rep = simulate_replay(ex["listening"], res["rules"], ex["traffic"])
        self.assertTrue(rep["all_correct"])
        self.assertEqual(rep["cpu_frames"], 700)
        audit = audit_all_sources(ex["listening"], res["rules"])
        self.assertTrue(audit["all_correct"])

    def test_seed_feasible_and_binds_cpu_cap_on_three_port_group(self):
        res = solve(SEED["listening"], SEED["traffic"], SEED["limits"])
        self.assertTrue(res["feasible"], res.get("reason"))
        self.assertLessEqual(res["entries"], 8)
        self.assertEqual(res["cpu_frames"], 300)
        # The three-port group is the only CPU traffic.
        cpu_rows = [r for r in simulate_replay(
            SEED["listening"], res["rules"], SEED["traffic"])["rows"]
            if r["via"] == "cpu"]
        self.assertEqual(len(cpu_rows), 1)
        self.assertEqual(cpu_rows[0]["group"], "239.10.2.2")
        self.assertEqual(cpu_rows[0]["actual_ports"], ["1", "2", "3"])

    def test_hardware_cap_can_make_problem_infeasible(self):
        # Two independent 2-port groups each carrying traffic where the CPU
        # cap is 0 -> each needs at least one entry -> hw cap 1 fails.
        listening = [
            {"source": None, "group": "239.1.0.1", "ports": ["1", "2"]},
            {"source": None, "group": "239.1.0.2", "ports": ["3", "4"]},
        ]
        traffic = [
            {"source": "10.0.0.1", "group": "239.1.0.1", "frames": 10},
            {"source": "10.0.0.2", "group": "239.1.0.2", "frames": 10},
        ]
        res = solve(listening, traffic, {"hw": 1, "cpu": 0})
        self.assertFalse(res["feasible"])
        self.assertGreater(res["min_entries_for_cpu_cap"], 1)

    def test_zero_cpu_cap_offloads_all_encodable_flows(self):
        listening = [
            {"source": None, "group": "239.5.5.5", "ports": ["1"]},
        ]
        traffic = [
            {"source": "10.0.0.1", "group": "239.5.5.5", "frames": 5},
            {"source": "10.0.0.2", "group": "239.5.5.5", "frames": 9},
        ]
        res = solve(listening, traffic, {"hw": 10, "cpu": 0})
        self.assertTrue(res["feasible"])
        # Aggregate one entry covers both sources.
        self.assertEqual(res["entries"], 1)
        self.assertEqual(res["cpu_frames"], 0)

    def test_unknown_group_zero_port_star_can_offload_drop(self):
        listening = []
        traffic = [
            {"source": "10.0.0.1", "group": "239.6.6.6", "frames": 50},
        ]
        res = solve(listening, traffic, {"hw": 1, "cpu": 0})
        self.assertTrue(res["feasible"], res.get("reason"))
        rule = res["rules"][0]
        self.assertIsNone(rule["source"])
        self.assertEqual(rule["ports"], [])  # 0 egress ports = hw drop
        rep = simulate_replay([], res["rules"], traffic)
        self.assertTrue(rep["all_correct"])
        self.assertEqual(rep["cpu_frames"], 0)


if __name__ == "__main__":
    unittest.main()
