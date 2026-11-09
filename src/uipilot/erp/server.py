"""在后台线程里起一个 ERP 实例，评测脚本用它拿到 base_url、重置数据、读数据库状态。"""

import json
import socket
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

import uvicorn

from .app import create_app


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class ERPServer:
    def __init__(self, port: int | None = None, db_dir: str | None = None):
        self.port = port or _free_port()
        self.base_url = f"http://127.0.0.1:{self.port}"
        db_dir = db_dir or tempfile.mkdtemp(prefix="uipilot-erp-")
        self.app = create_app(str(Path(db_dir) / "erp.db"))
        config = uvicorn.Config(self.app, host="127.0.0.1", port=self.port, log_level="warning")
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._server.run, daemon=True)

    def __enter__(self) -> "ERPServer":
        self._thread.start()
        for _ in range(100):
            if self._server.started:
                return self
            time.sleep(0.05)
        raise RuntimeError("ERP 服务没有启动")

    def __exit__(self, *exc) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=5)

    def _post(self, path: str, body: dict) -> dict:
        req = urllib.request.Request(
            self.base_url + path,
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req) as r:
            return json.loads(r.read())

    def reset(self, bugs: list[str] | None = None) -> None:
        self._post("/__admin/reset", {"bugs": bugs or []})

    def state(self) -> dict:
        with urllib.request.urlopen(self.base_url + "/__admin/state") as r:
            return json.loads(r.read())
