import unittest

from app.dataplane import audit_all_sources, forward_frame, make_indexes, simulate_replay


LISTENING = [
    {"source": None, "group": "239.1.1.1", "ports": ["1", "2"]},
    {"source": "10.0.0.1", "group": "239.1.1.1", "ports": ["3"]},
    {"source": None, "group": "239.2.2.2", "ports": ["1", "2", "3"]},
]


class DataPlaneTest(unittest.TestCase):
    def test_sg_priority_over_star_hardware(self):
        rules = [
            {"source": None, "group": "239.1.1.1", "action": "ports", "ports": ["1", "2"]},
            {"source": "10.0.0.1", "group": "239.1.1.1", "action": "ports", "ports": ["3"]},
        ]
        l_idx, h_idx = make_indexes(LISTENING, rules)

        r1 = forward_frame(l_idx, h_idx, "10.0.0.1", "239.1.1.1")
        self.assertEqual(r1["via"], "hardware")
        self.assertEqual(r1["hw_match"], "S,G")
        self.assertEqual(r1["actual_ports"], ["3"])
        self.assertTrue(r1["correct"])

        r2 = forward_frame(l_idx, h_idx, "10.0.0.9", "239.1.1.1")
        self.assertEqual(r2["via"], "hardware")
        self.assertEqual(r2["hw_match"], "*,G")
        self.assertEqual(r2["actual_ports"], ["1", "2"])
        self.assertTrue(r2["correct"])

    def test_miss_goes_cpu_and_matches_listening(self):
        l_idx, h_idx = make_indexes(LISTENING, [])
        r = forward_frame(l_idx, h_idx, "10.0.0.9", "239.1.1.1")
        self.assertEqual(r["via"], "cpu")
        self.assertEqual(r["cpu_reason"], "miss")
        self.assertEqual(r["actual_ports"], ["1", "2"])
        self.assertTrue(r["correct"])

    def test_three_port_group_forced_cpu(self):
        l_idx, h_idx = make_indexes(LISTENING, [])
        r = forward_frame(l_idx, h_idx, "172.16.0.10", "239.2.2.2")
        self.assertEqual(r["via"], "cpu")
        self.assertEqual(r["actual_ports"], ["1", "2", "3"])
        self.assertEqual(r["required_ports"], ["1", "2", "3"])
        self.assertTrue(r["correct"])

    def test_hw_cpu_action_uses_listening_table(self):
        rules = [
            {"source": "10.0.0.1", "group": "239.1.1.1", "action": "cpu", "ports": []},
        ]
        l_idx, h_idx = make_indexes(LISTENING, rules)
        r = forward_frame(l_idx, h_idx, "10.0.0.1", "239.1.1.1")
        self.assertEqual(r["via"], "cpu")
        self.assertEqual(r["cpu_reason"], "hw_action")
        self.assertEqual(r["actual_ports"], ["3"])
        self.assertTrue(r["correct"])

    def test_wrong_hardware_ports_flagged(self):
        rules = [
            {"source": None, "group": "239.1.1.1", "action": "ports", "ports": ["9"]},
        ]
        l_idx, h_idx = make_indexes(LISTENING, rules)
        # Generic source gets wrong hardware egress.
        r = forward_frame(l_idx, h_idx, "8.8.8.8", "239.1.1.1")
        self.assertEqual(r["via"], "hardware")
        self.assertEqual(r["actual_ports"], ["9"])
        self.assertFalse(r["correct"])

    def test_unknown_group_cpu_drops_with_empty_ports(self):
        l_idx, h_idx = make_indexes(LISTENING, [])
        r = forward_frame(l_idx, h_idx, "8.8.4.4", "239.9.9.9")
        self.assertEqual(r["via"], "cpu")
        self.assertEqual(r["actual_ports"], [])
        self.assertEqual(r["required_ports"], [])
        self.assertTrue(r["correct"])

    def test_replay_cpu_total_is_frame_weighted(self):
        flows = [
            {"source": "10.0.0.9", "group": "239.1.1.1", "frames": 100},  # cpu, no rules
            {"source": "1.1.1.1", "group": "239.2.2.2", "frames": 7},      # cpu
        ]
        out = simulate_replay(LISTENING, [], flows)
        self.assertEqual(out["total_frames"], 107)
        self.assertEqual(out["cpu_frames"], 107)
        self.assertEqual(out["hardware_frames"], 0)
        self.assertTrue(out["all_correct"])

        rules = [
            {"source": None, "group": "239.1.1.1", "action": "ports", "ports": ["1", "2"]},
        ]
        out = simulate_replay(LISTENING, rules, flows)
        self.assertEqual(out["cpu_frames"], 7)
        self.assertEqual(out["hardware_frames"], 100)

    def test_audit_catches_bad_star_for_generic_source(self):
        bad_rules = [
            {"source": None, "group": "239.1.1.1", "action": "ports", "ports": ["1"]},
        ]
        audit = audit_all_sources(LISTENING, bad_rules)
        self.assertFalse(audit["all_correct"])
        self.assertGreater(audit["problem_count"], 0)

    def test_audit_accepts_correct_config(self):
        rules = [
            {"source": None, "group": "239.1.1.1", "action": "ports", "ports": ["1", "2"]},
            {"source": "10.0.0.1", "group": "239.1.1.1", "action": "ports", "ports": ["3"]},
        ]
        audit = audit_all_sources(LISTENING, rules,
                                  extra_sources=["10.0.0.77"])
        self.assertTrue(audit["all_correct"], audit["probes"])


if __name__ == "__main__":
    unittest.main()
