"""黄河影像征集流转的运行入口：健康检查 + 征集后端 API。"""

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from riverflow import BlobStore, RiverflowService, load_policy
from riverflow.api import Api
from riverflow.core import DomainError

SERVICE_ID = "river-media-flow"
SERVICE_NAME = "黄河影像征集流转"


def health_payload():
    """返回稳定的服务身份信息。"""
    return {"status": "ok", "service": SERVICE_ID, "name": SERVICE_NAME}


def build_api(data_dir=".data/blobs", policy_path="fixtures/sample.json"):
    service = RiverflowService(BlobStore(data_dir), load_policy(policy_path))
    return Api(service)


class Handler(BaseHTTPRequestHandler):
    """健康检查与征集后端 API。"""

    api = None  # 由 main() 注入

    def _send(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _dispatch(self, method):
        if self.path == "/health" and method == "GET":
            self._send(200, health_payload())
            return
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            self._send(400, {"error": "请求体须为 JSON"})
            return
        try:
            status, payload = self.api.handle(method, self.path, body)
        except DomainError as exc:
            self._send(exc.status, {"error": str(exc)})
            return
        if self.path == "/health":
            payload = health_payload()
        self._send(status, payload)

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_PUT(self):
        self._dispatch("PUT")

    def log_message(self, *_args):
        return


def main():
    parser = argparse.ArgumentParser(description=SERVICE_NAME)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--data-dir", default=".data/blobs")
    args = parser.parse_args()
    if args.check:
        assert health_payload()["service"] == SERVICE_ID
        api = build_api(data_dir=args.data_dir)
        assert api.svc.policy["minimum_video_height"] >= 1080
        print("基础检查通过")
        return
    Handler.api = build_api(data_dir=args.data_dir)
    ThreadingHTTPServer(("0.0.0.0", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
