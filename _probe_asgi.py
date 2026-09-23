# -*- coding: utf-8 -*-
"""临时探针：验证不依赖 httpx/fastapi.testclient 的 in-process ASGI 请求是否可行。用完即删。"""
import asyncio
import importlib.util
import os
import sys
import tempfile

BASE = os.path.dirname(os.path.abspath(__file__))
BULK_DIR = os.path.join(BASE, "bulk_api")
sys.path.insert(0, BASE)

import runtime_config  # noqa: E402

runtime_config.ensure_env_loaded()
os.environ["API_KEY"] = "probe_key_123"
os.environ["BULK_DB_FILE"] = tempfile.mktemp(prefix="probe_", suffix=".sqlite3")

spec = importlib.util.spec_from_file_location(
    "bulk_api_module", os.path.join(BULK_DIR, "bulk_api.py"))
mod = importlib.util.module_from_spec(spec)
sys.path.insert(0, BULK_DIR)
spec.loader.exec_module(mod)
sys.path.remove(BULK_DIR)


def status(app, path, key=None, query=b""):
    headers = [(b"host", b"127.0.0.1")]
    if key:
        headers.append((b"x-api-key", key.encode()))
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": query,
        "root_path": "",
        "headers": headers,
        "client": ("127.0.0.1", 12345),
        "server": ("127.0.0.1", 8020),
    }
    out = {}

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        if message["type"] == "http.response.start":
            out["status"] = message["status"]

    asyncio.run(app(scope, receive, send))
    return out.get("status")


print("no key   ->", status(mod.app, "/shops"))
print("with key ->", status(mod.app, "/shops", "probe_key_123"))
print("bad key  ->", status(mod.app, "/shops", "nope"))
print("health   ->", status(mod.app, "/health"))
print("query    ->", status(mod.app, "/shops", None, b"api_key=probe_key_123"))
