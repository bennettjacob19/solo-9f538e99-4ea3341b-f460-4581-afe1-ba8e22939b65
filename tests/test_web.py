import os
import shutil
import tempfile
import unittest

from werkzeug.datastructures import MultiDict

from app import create_app


def drill_form(ex: dict) -> MultiDict:
    pairs = []
    for e in ex["listening"]:
        pairs.append(("l_source", e.get("source") or ""))
        pairs.append(("l_group", e["group"]))
        pairs.append(("l_ports", ", ".join(e["ports"])))
    for f in ex["traffic"]:
        pairs.append(("t_source", f["source"]))
        pairs.append(("t_group", f["group"]))
        pairs.append(("t_frames", str(f["frames"])))
    pairs.append(("hw_limit", str(ex["limits"]["hw"])))
    pairs.append(("cpu_limit", str(ex["limits"]["cpu"])))
    if ex.get("simulate_fail"):
        pairs.append(("simulate_fail", "1"))
    return MultiDict(pairs)


class WebTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="drill-web-")
        self.app = create_app({
            "TESTING": True,
            "DB_PATH": os.path.join(self.tmp, "web.db"),
            "PUBLISH_DELAY": 0,
        })
        self.client = self.app.test_client()

    def tearDown(self):
        self.app.extensions["drill_storage"].close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_pages_render(self):
        for path in ["/", "/rules", "/replay", "/revisions",
                     "/revisions/1"]:
            r = self.client.get(path)
            self.assertEqual(r.status_code, 200, path)
            self.assertIn("组播".encode(), r.data)

    def test_trial_three_ports_via_html_form(self):
        r = self.client.post("/send", data={
            "source": "198.51.100.9",
            "group": "239.10.2.2",
        })
        self.assertEqual(r.status_code, 200)
        text = r.data.decode()
        self.assertIn("CPU 转发", text)
        self.assertIn("端口完全一致", text)
        # Three egress ports rendered as chips.
        for p in [">1<", ">2<", ">3<"]:
            self.assertIn(p, text)

    def test_replay_form_shows_cpu_total_300(self):
        r = self.client.post("/replay/run", data={"revision_id": "1"})
        self.assertEqual(r.status_code, 302)
        loc = r.headers["Location"]
        self.assertIn("/replay?run=", loc)
        page = self.client.get(loc)
        self.assertEqual(page.status_code, 200)
        text = page.data.decode()
        self.assertIn("回放 CPU 帧总数", text)
        self.assertIn(">300<", text)

    def test_submit_feasible_form_redirects_to_new_revision(self):
        from app.examples import THREE_PORTS
        r = self.client.post("/drills", data=drill_form(THREE_PORTS))
        self.assertEqual(r.status_code, 302)
        self.assertRegex(r.headers["Location"], r"/revisions/2\?new=1")

    def test_submit_impossible_form_renders_422_and_keeps_active(self):
        from app.examples import IMPOSSIBLE
        r = self.client.post("/drills", data=drill_form(IMPOSSIBLE))
        self.assertEqual(r.status_code, 422)
        text = r.data.decode()
        self.assertIn("无解", text)
        self.assertIn("继续使用 SQLite 中已发布的配置", text)

        # Active is still seed v1.
        cfg = self.client.get("/api/config").get_json()["snapshot"]
        self.assertEqual(cfg["version"], 1)

    def test_submit_publish_failure_renders_502(self):
        from app.examples import PUBLISH_FAIL
        r = self.client.post("/drills", data=drill_form(PUBLISH_FAIL))
        self.assertEqual(r.status_code, 502)
        text = r.data.decode()
        self.assertIn("模拟发布失败", text)
        self.assertIn("继续使用 SQLite 中已发布的配置", text)

    def test_validation_error_renders_400(self):
        data = drill_form({
            "listening": [{"source": "bad ip", "group": "239.1.1.1",
                           "ports": "1"}],
            "traffic": [],
            "limits": {"hw": 1, "cpu": 1},
        })
        r = self.client.post("/drills", data=data)
        self.assertEqual(r.status_code, 400)
        self.assertIn("输入校验未通过", r.data.decode())


if __name__ == "__main__":
    unittest.main()
