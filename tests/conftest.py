"""pytest 公共夹具与辅助：应用实例、素材构造、直接投稿流程、HTTP 客户端。"""

from __future__ import annotations

import json
import threading
import urllib.request
import urllib.error
from pathlib import Path

import pytest

from riverflow import demo
from riverflow.app import Application
from riverflow.api import make_server
from riverflow.config import TERMS_TEXT, CampaignConfig

TERMS_VERSION = "terms-2026-01"
CHANNELS = ("campaign-page", "newspaper", "media-matrix")


def make_config() -> CampaignConfig:
    return CampaignConfig(
        campaign_id="river-test",
        accepted_media=("image", "video", "story"),
        minimum_video_height=1080,
        channels=CHANNELS,
        ai_generated_allowed=False,
        terms_version=TERMS_VERSION,
        terms_text=TERMS_TEXT,
    )


@pytest.fixture
def app(tmp_path):
    application = Application(make_config(), tmp_path / "events.sqlite",
                              tmp_path / "storage")
    yield application
    application.close()


@pytest.fixture
def server(app):
    httpd, _api = make_server(app, "127.0.0.1", 0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    host, port = httpd.server_address
    base = f"http://{host}:{port}"
    try:
        yield base
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)


class HttpClient:
    def __init__(self, base: str):
        self.base = base

    def request(self, method: str, path: str, body=None, headers=None,
                raw: bytes | None = None):
        url = self.base + path
        data = None
        hdrs = dict(headers or {})
        if raw is not None:
            data = raw
        elif body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            hdrs.setdefault("Content-Type", "application/json")
        req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
        try:
            with urllib.request.urlopen(req) as resp:
                payload = resp.read()
                return resp.status, json.loads(payload.decode("utf-8")) if payload else {}
        except urllib.error.HTTPError as e:
            payload = e.read()
            try:
                return e.code, json.loads(payload.decode("utf-8"))
            except json.JSONDecodeError:
                return e.code, {"raw": payload.decode("utf-8", "replace")}


@pytest.fixture
def http(server):
    return HttpClient(server)


# ---- 直接构造素材与投稿 ----------------------------------------------------


def png_bytes(width: int = 2000, height: int = 1500, seed: int = 1,
              shift=(0, 0)) -> bytes:
    """生成测试图片；shift 非零时表示同一底图在该偏移处的裁剪。"""
    import tempfile
    if shift == (0, 0):
        path = Path(tempfile.mkdtemp()) / "p.png"
        demo.write_png(path, width, height,
                       demo.river_photo(width, height, seed))
    else:
        path = Path(tempfile.mkdtemp()) / "p.png"
        src_fn = demo.river_photo(width + shift[0], height + shift[1], seed)

        def px(x, y, _fn=src_fn, _s=shift):
            return _fn(x + _s[0], y + _s[1])

        def row_bytes(y, _fn=src_fn, _s=shift):
            row = _fn.row_bytes(y + _s[1])
            return row[_s[0] * 3:(_s[0] + width) * 3]

        px.row_bytes = row_bytes
        demo.write_png(path, width, height, px)
    data = path.read_bytes()
    return data


def video_bytes(width=1920, height=1080, duration=45, tmp_dir="/tmp"):
    path = Path(tmp_dir) / f"_rf_video_{width}x{height}.mp4"
    demo.write_video_stub(path, width, height, duration)
    data = path.read_bytes()
    meta = json.loads(Path(str(path) + ".media.json").read_text("utf-8"))
    path.unlink(missing_ok=True)
    Path(str(path) + ".media.json").unlink(missing_ok=True)
    return data, meta


def chunked_upload(app, data: bytes, media_type: str, filename: str,
                   contact: str, n: int = 4, media_meta=None,
                   declared_sha256=None):
    size = (len(data) + n - 1) // n
    sess = app.upload.init_upload(
        uploader=contact, media_type=media_type, filename=filename,
        total_chunks=n, chunk_size=size,
        declared_sha256=declared_sha256,
        media_meta=media_meta)
    sid = sess["session_id"]
    for i in range(n):
        app.upload.put_chunk(sid, i, data[i * size:(i + 1) * size])
    return sid


def submit(app, data: bytes, media_type: str = "image", filename: str = "p.png",
           display_name="测试作者", contact="author@example.org",
           title="黄河作品", n: int = 4, media_meta=None,
           declared_sha256=None, note="") -> dict:
    sid = chunked_upload(app, data, media_type, filename, contact, n=n,
                         media_meta=media_meta, declared_sha256=declared_sha256)
    return app.upload.finalize(
        sid, {"display_name": display_name, "contact": contact},
        terms_version=TERMS_VERSION, title=title, note=note)


def close_all_reviews(app, sid: str, reviewer: str = "reviewer-qin",
                      verdict: str = "authentic") -> None:
    """把某投稿件全部未决人工核验以同一结论关闭（测试辅助）。"""
    s = app.proj.submissions[sid]
    for rid in list(s.open_review_ids):
        app.editorial.record_verdict(rid, reviewer, "reviewer", verdict, "测试核验")


def staff_headers(staff_id: str, role: str, idem: str | None = None) -> dict:
    h = {"X-Staff-Id": staff_id, "X-Staff-Role": role}
    if idem:
        h["Idempotency-Key"] = idem
    return h
