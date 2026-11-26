import os
import shutil
import tempfile
import threading
import time
import unittest

from app import create_app
from app.examples import THREE_PORTS
from app.publisher import PublishError


class BlockingPublisher:
    """Publisher whose push blocks until a test releases it."""

    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()

    def push(self, snapshot, *, simulate_fail=False):
        self.entered.set()
        if simulate_fail:
            raise PublishError("simulated")
        if not self.release.wait(timeout=5):
            raise RuntimeError("test release timeout")
        return {"transaction": "blocktest", "entry_count": len(snapshot["rules"])}


class ConcurrencyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="drill-conc-")
        self.app = create_app({
            "TESTING": True,
            "DB_PATH": os.path.join(self.tmp, "c.db"),
            "PUBLISH_DELAY": 0,
        })
        self.client = self.app.test_client()
        self.blocking = BlockingPublisher()
        self.app.extensions["drill_service"].publisher = self.blocking

    def tearDown(self):
        self.blocking.release.set()
        self.app.extensions["drill_storage"].close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_replay_served_while_publish_blocked(self):
        result_box = {}

        def publish():
            result_box["result"] = self.app.extensions["drill_service"].submit_drill(
                dict(THREE_PORTS))

        t = threading.Thread(target=publish)
        t.start()
        self.assertTrue(self.blocking.entered.wait(timeout=2),
                        "publish did not reach the device push")

        # Publish is in flight (process publish lock held, push blocked).
        # Replay must still be served and use the old published revision #1.
        t0 = time.time()
        r = self.client.post("/api/replay", json={})
        elapsed = time.time() - t0
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertEqual(body["revision_id"], 1)
        self.assertEqual(body["version"], 1)
        self.assertEqual(body["cpu_frames"], 300)
        self.assertLess(elapsed, 2.0, "replay was blocked by publishing")

        # Trial send works too during the switch.
        r2 = self.client.post("/api/send", json={
            "source": "8.8.8.8", "group": "239.10.2.2"})
        self.assertEqual(r2.status_code, 200)
        self.assertEqual(r2.get_json()["version"], 1)

        self.blocking.release.set()
        t.join(timeout=3)
        self.assertFalse(t.is_alive())
        self.assertTrue(result_box["result"]["ok"])

        cfg = self.client.get("/api/config").get_json()["snapshot"]
        self.assertEqual(cfg["version"], 2)


if __name__ == "__main__":
    unittest.main()
