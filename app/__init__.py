"""Flask application factory for the multicast hardware-table drill page."""
from __future__ import annotations

import os

from flask import Flask

from .examples import EXAMPLES
from .publisher import Publisher
from .services import DrillService
from .storage import Storage

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def create_app(config: dict | None = None) -> Flask:
    app = Flask(__name__)
    default_db = os.path.join(PROJECT_ROOT, "data", "drill.db")
    app.config.update(
        DB_PATH=os.environ.get("DB_PATH", default_db),
        PUBLISH_DELAY=float(os.environ.get("PUBLISH_DELAY", "0.6")),
        PUBLISH_FAIL_RATE=float(os.environ.get("PUBLISH_FAIL_RATE", "0")),
        JSON_AS_ASCII=False,
    )
    if config:
        app.config.update(config)

    storage = Storage(app.config["DB_PATH"])
    publisher = Publisher(
        delay=app.config["PUBLISH_DELAY"],
        fail_rate=app.config["PUBLISH_FAIL_RATE"],
    )
    service = DrillService(storage, publisher)
    app.extensions["drill_service"] = service
    app.extensions["drill_storage"] = storage

    from .routes import bp
    app.register_blueprint(bp)

    _seed_if_empty(service)

    return app


def _seed_if_empty(service: DrillService) -> None:
    if service.active_revision() is not None:
        return
    seed = EXAMPLES["seed"]
    result = service.submit_drill(dict(seed))
    if not result.get("ok"):
        raise RuntimeError(f"种子配置发布失败: {result}")
