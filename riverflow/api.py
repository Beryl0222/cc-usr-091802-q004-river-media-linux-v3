"""HTTP API（标准库实现，零额外依赖）。

鉴权约定（与反向代理/SSO 对接前的内置方案）：
* 员工接口要求 ``X-Staff-Id`` 与 ``X-Staff-Role`` 头，角色必须是
  editor/reviewer/publisher/admin，服务端按接口做职责分离校验；
* 投稿人操作凭 finalize 时返回的一次性 contributor_token（头
  ``X-Contributor-Token`` 或 JSON 字段）；
* 公开接口无需鉴权，且只返回脱敏数据。

写接口支持 ``Idempotency-Key`` 头：同键重复请求返回首次结果，
配合续传与渠道回调保证"最多生效一次"。
"""

from __future__ import annotations

import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import urlparse, parse_qs

from .domain import STAFF_ROLES
from .eventstore import ConflictError

_MAX_BODY = 8 * 1024 * 1024  # 大文件走分片；单片上限 8 MiB（可由部署覆盖）


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


class Router:
    def __init__(self):
        self.routes: list[tuple[str, re.Pattern, Callable]] = []

    def add(self, method: str, pattern: str, handler: Callable):
        self.routes.append((method, re.compile("^" + pattern + "$"), handler))

    def match(self, method: str, path: str):
        for m, rx, fn in self.routes:
            if m != method:
                continue
            match = rx.match(path)
            if match:
                return fn, match.groupdict()
        return None, None


class Api:
    def __init__(self, app, max_chunk_bytes: int = _MAX_BODY):
        self.app = app
        self.max_chunk_bytes = max_chunk_bytes
        self.router = Router()
        self._idem_locks: dict[str, threading.Lock] = {}
        self._idem_locks_guard = threading.Lock()
        self._register()

    # ---- 路由表 ---------------------------------------------------------

    def _register(self):
        r = self.router
        r.add("GET", r"/health", lambda h, p: self.health(h))
        r.add("GET", r"/terms", lambda h, p: self.terms(h))
        # 续传
        r.add("POST", r"/v1/uploads", lambda h, p: self.upload_init(h))
        r.add("GET", r"/v1/uploads/(?P<sid>[0-9a-f]+)", lambda h, p: self.upload_status(h, p["sid"]))
        r.add("PUT", r"/v1/uploads/(?P<sid>[0-9a-f]+)/chunks/(?P<index>\d+)",
              lambda h, p: self.upload_chunk(h, p["sid"], int(p["index"])))
        r.add("POST", r"/v1/uploads/(?P<sid>[0-9a-f]+)/finalize",
              lambda h, p: self.upload_finalize(h, p["sid"]))
        # 投稿人
        r.add("POST", r"/v1/submissions/(?P<sid>[0-9a-f]+)/notes",
              lambda h, p: self.note_add(h, p["sid"]))
        r.add("POST", r"/v1/submissions/(?P<sid>[0-9a-f]+)/replace",
              lambda h, p: self.file_replace(h, p["sid"]))
        r.add("POST", r"/v1/submissions/(?P<sid>[0-9a-f]+)/withdraw",
              lambda h, p: self.withdraw(h, p["sid"]))
        r.add("POST", r"/v1/submissions/(?P<sid>[0-9a-f]+)/rights-claims",
              lambda h, p: self.rights_claim(h, p["sid"]))
        # 公开
        r.add("GET", r"/public/catalog", lambda h, p: self.public_catalog(h))
        r.add("GET", r"/public/status", lambda h, p: self.public_status(h))
        # 员工
        r.add("GET", r"/v1/staff/inbox", lambda h, p: self.staff_inbox(h))
        r.add("GET", r"/v1/staff/reviews/open", lambda h, p: self.reviews_open(h))
        r.add("GET", r"/v1/staff/queue", lambda h, p: self.publish_queue(h))
        r.add("GET", r"/v1/staff/submissions/(?P<sid>[0-9a-f]+)/dossier",
              lambda h, p: self.dossier(h, p["sid"]))
        r.add("POST", r"/v1/staff/detections", lambda h, p: self.report_detection(h))
        r.add("POST", r"/v1/staff/reviews/(?P<rid>[0-9a-f]+)/verdict",
              lambda h, p: self.review_verdict(h, p["rid"]))
        r.add("POST",
              r"/v1/staff/submissions/(?P<sid>[0-9a-f]+)/claims/(?P<cid>[0-9a-f]+)/resolve",
              lambda h, p: self.claim_resolve(h, p["sid"], p["cid"]))
        r.add("POST", r"/v1/staff/submissions/(?P<sid>[0-9a-f]+)/withdrawal/accept",
              lambda h, p: self.withdrawal_accept(h, p["sid"]))
        r.add("POST", r"/v1/staff/selections", lambda h, p: self.select(h))
        r.add("POST", r"/v1/staff/selections/(?P<lid>[0-9a-f]+)/approve",
              lambda h, p: self.select_approve(h, p["lid"]))
        r.add("POST", r"/v1/staff/selections/(?P<lid>[0-9a-f]+)/reject",
              lambda h, p: self.select_reject(h, p["lid"]))
        r.add("POST", r"/v1/staff/selections/(?P<lid>[0-9a-f]+)/dispatch",
              lambda h, p: self.dispatch(h, p["lid"]))
        r.add("POST", r"/v1/staff/deliveries/(?P<did>[0-9a-f]+)/result",
              lambda h, p: self.delivery_result(h, p["did"]))
        r.add("POST", r"/v1/staff/deliveries/(?P<did>[0-9a-f]+)/withdraw",
              lambda h, p: self.delivery_withdraw(h, p["did"]))
        r.add("POST", r"/v1/staff/deliveries/(?P<did>[0-9a-f]+)/layout",
              lambda h, p: self.delivery_layout(h, p["did"]))

    # ---- 处理器实现：公开 ----------------------------------------------

    def health(self, h):
        from . import SERVICE_ID, SERVICE_NAME
        return 200, {"status": "ok", "service": SERVICE_ID, "name": SERVICE_NAME}

    def terms(self, h):
        t = self.app.terms_snapshot()
        return 200, {"campaign_id": self.app.config.campaign_id,
                     "terms_version": t["version"],
                     "terms_digest": t["digest"],
                     "non_commercial_only": True,
                     "text": t["text"]}

    def public_catalog(self, h):
        return 200, {"campaign_id": self.app.config.campaign_id,
                     "items": self.app.public_catalog()}

    def public_status(self, h):
        qs = parse_qs(urlparse(h.path).query)
        code = (qs.get("code") or [""])[0]
        if not code:
            raise ApiError(400, "缺少 code 参数")
        view = self.app.public_status(code)
        if view is None:
            raise ApiError(404, "查询编号不存在")
        return 200, view

    # ---- 续传 -----------------------------------------------------------

    def upload_init(self, h):
        b = h.read_json()
        res = self.app.upload.init_upload(
            uploader=b.get("uploader", "anonymous"),
            media_type=b["media_type"], filename=b["filename"],
            total_chunks=int(b["total_chunks"]), chunk_size=int(b["chunk_size"]),
            declared_sha256=b.get("declared_sha256"),
            media_meta=b.get("media_meta"))
        return 201, res

    def upload_status(self, h, sid):
        return 200, self.app.upload.status(sid)

    def upload_chunk(self, h, sid, index):
        data = h.read_body(self.max_chunk_bytes)
        res = self.app.upload.put_chunk(sid, index, data)
        return 200, res

    def upload_finalize(self, h, sid):
        b = h.read_json()
        res = self.app.upload.finalize(
            sid,
            contributor=b["contributor"],
            terms_version=b["terms_version"],
            title=b.get("title", ""), note=b.get("note", ""),
            declared_original=b.get("declared_original", True),
            ai_assist=b.get("ai_assist", "none"),
            replaces_submission_id=b.get("replaces_submission_id"))
        return 201, res

    # ---- 投稿人事件 -----------------------------------------------------

    def note_add(self, h, sid):
        b = h.read_json()
        self.app.upload.supplement_note(sid, h.contributor_token(b), b["note"])
        return 201, {"submission_id": sid, "event": "NoteSupplemented"}

    def file_replace(self, h, sid):
        b = h.read_json()
        res = self.app.upload.replace_from_session(
            sid, h.contributor_token(b), b["session_id"], b.get("reason", ""))
        return 201, res

    def withdraw(self, h, sid):
        b = h.read_json()
        self.app.upload.request_withdrawal(
            sid, h.contributor_token(b), b.get("reason", ""))
        return 201, {"submission_id": sid, "event": "WithdrawalRequested"}

    def rights_claim(self, h, sid):
        b = h.read_json()
        claim_id = self.app.upload.raise_rights_claim(
            sid, b["claimant"], b.get("contact", ""), b["note"])
        return 201, {"claim_id": claim_id, "review": "manual-review-required"}

    # ---- 员工视图与操作 -------------------------------------------------

    def staff_inbox(self, h):
        h.require_staff()
        return 200, {"items": self.app.inbox()}

    def reviews_open(self, h):
        h.require_staff()
        return 200, {"items": self.app.reviews_open()}

    def publish_queue(self, h):
        h.require_staff()
        return 200, {"queue": self.app.publishable_queue()}

    def dossier(self, h, sid):
        h.require_staff()
        try:
            return 200, self.app.dossier(sid)
        except KeyError:
            raise ApiError(404, "投稿件不存在")

    def report_detection(self, h):
        staff, _role = h.require_staff()
        b = h.read_json()
        rid = self.app.detection.report_external(
            b["submission_id"], b["kind"], b.get("score"),
            b.get("evidence", {}), staff, b.get("sha256"))
        return 201, {"review_id": rid, "policy": "signal-requires-manual-review",
                     "auto_qualification": False}

    def review_verdict(self, h, rid):
        staff, role = h.require_staff()
        b = h.read_json()
        self.app.editorial.record_verdict(
            rid, staff, role, b["verdict"], b.get("note", ""))
        return 201, {"review_id": rid, "verdict": b["verdict"]}

    def claim_resolve(self, h, sid, cid):
        staff, role = h.require_staff()
        b = h.read_json()
        self.app.editorial.resolve_rights_claim(
            sid, cid, staff, role, b["resolution"], b.get("note", ""))
        return 201, {"claim_id": cid, "resolution": b["resolution"]}

    def withdrawal_accept(self, h, sid):
        staff, role = h.require_staff()
        b = h.read_json(silent=True) or {}
        res = self.app.editorial.accept_withdrawal(
            sid, staff, role, b.get("note", ""))
        return 201, res

    def select(self, h):
        staff, role = h.require_staff()
        b = h.read_json()
        lid = self.app.editorial.select(
            b["submission_id"], b["sha256"], b["channels"],
            staff, role, b.get("note", ""))
        return 201, {"selection_id": lid}

    def select_approve(self, h, lid):
        staff, role = h.require_staff()
        b = h.read_json(silent=True) or {}
        self.app.editorial.approve_selection(lid, staff, role, b.get("note", ""))
        return 201, {"selection_id": lid, "approved": True}

    def select_reject(self, h, lid):
        staff, role = h.require_staff()
        b = h.read_json()
        self.app.editorial.reject_selection(lid, staff, role, b["reason"])
        return 201, {"selection_id": lid, "rejected": True}

    def dispatch(self, h, lid):
        staff, role = h.require_staff()
        ids = self.app.distribution.dispatch(lid, staff, role)
        return 201, {"selection_id": lid, "delivery_ids": ids}

    def delivery_result(self, h, did):
        staff, role = h.require_staff()
        if role not in ("publisher", "admin"):
            raise ApiError(403, "仅发布岗可登记渠道回执")
        b = h.read_json()
        res = self.app.distribution.record_result(
            did, b["result"], b["callback_id"],
            b.get("channel_ref"), b.get("detail"))
        return 200, res

    def delivery_withdraw(self, h, did):
        staff, role = h.require_staff()
        b = h.read_json()
        self.app.distribution.withdraw(
            did, staff, role, b["reason"], b.get("callback_id"))
        return 201, {"delivery_id": did, "state": "withdrawn"}

    def delivery_layout(self, h, did):
        staff, role = h.require_staff()
        b = h.read_json()
        self.app.editorial.enter_layout(
            did, staff, role, b["edition"], b["layout_ref"])
        return 201, {"delivery_id": did, "event": "VersionEnteredLayout",
                     "distribution_after_layout": "halt-new-distribution-on-withdrawal"}


def make_handler(api: Api):
    class Handler(BaseHTTPRequestHandler):
        server_version = "RiverMediaFlow/1.0"

        def _dispatch(self, method: str):
            parsed = urlparse(self.path)
            fn, kwargs = api.router.match(method, parsed.path)
            if fn is None:
                self._write_json(404, {"error": "not-found",
                                       "path": parsed.path})
                return
            try:
                key = self.headers.get("Idempotency-Key")
                if method == "POST" and key:
                    status, body = self._idempotent(key, parsed.path, fn, kwargs)
                else:
                    status, body = fn(self, kwargs)
            except ApiError as e:
                self._write_json(e.status, {"error": e.message})
            except ConflictError as e:
                self._write_json(409, {"error": str(e)})
            except (KeyError, ValueError) as e:
                self._write_json(400, {"error": str(e)})
            except Exception as e:  # 防御：不让栈穿透到外部
                self._write_json(500, {"error": "internal-error",
                                       "type": type(e).__name__})
            else:
                self._write_json(status, body)

        def _idempotent(self, key: str, path: str, fn, kwargs):
            scope = f"{self.headers.get('X-Staff-Id', 'anon')}:{path}"
            lock_key = scope + key
            with api._idem_locks_guard:
                lock = api._idem_locks.setdefault(lock_key, threading.Lock())
            with lock:
                cached = api.app.store.idem_get(scope, key)
                if cached is not None:
                    return cached["status"], cached["body"]
                status, body = fn(self, kwargs)
                with api.app.store.tx() as conn:
                    api.app.store.idem_put(conn, scope, key,
                                           {"status": status, "body": body})
                return status, body

        def do_GET(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

        def do_PUT(self):
            self._dispatch("PUT")

        # ---- 工具 -------------------------------------------------------

        def read_body(self, limit: int) -> bytes:
            n = int(self.headers.get("Content-Length", 0))
            if n > limit:
                raise ApiError(413, f"请求体超过上限 {limit} 字节")
            return self.rfile.read(n) if n else b""

        def read_json(self, silent: bool = False) -> dict[str, Any]:
            raw = self.read_body(2 * 1024 * 1024)
            if silent and not raw:
                return {}
            try:
                data = json.loads(raw.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError) as e:
                raise ApiError(400, f"JSON 解析失败: {e}")
            if not isinstance(data, dict):
                raise ApiError(400, "请求体必须是 JSON 对象")
            return data

        def require_staff(self) -> tuple[str, str]:
            staff = self.headers.get("X-Staff-Id", "").strip()
            role = self.headers.get("X-Staff-Role", "").strip()
            if not staff or not role:
                raise ApiError(401, "缺少员工身份头 X-Staff-Id/X-Staff-Role")
            if role not in STAFF_ROLES:
                raise ApiError(403, f"未知角色: {role}")
            return staff, role

        def contributor_token(self, body: dict[str, Any]) -> str:
            token = self.headers.get("X-Contributor-Token") or body.get("token")
            if not token:
                raise ApiError(401, "缺少投稿人凭证 X-Contributor-Token")
            return token

        def _write_json(self, status: int, body: Any):
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *_args):
            return

    return Handler


def make_server(app, host: str, port: int, max_chunk_bytes: int = _MAX_BODY):
    api = Api(app, max_chunk_bytes=max_chunk_bytes)
    return ThreadingHTTPServer((host, port), make_handler(api)), api
