"""HTTP API：把领域服务暴露为 JSON 接口，handler 保持薄层。"""

from __future__ import annotations

import base64
import binascii

from .core import DomainError


def _b64(value, field):
    try:
        return base64.b64decode(value or "", validate=True)
    except (binascii.Error, TypeError):
        raise DomainError(f"{field} 须为 base64 编码")


class Api:
    def __init__(self, service):
        self.svc = service

    def handle(self, method, path, body):
        """返回 (status, payload)。body 为已解析的 JSON 对象。"""
        body = body or {}
        parts = [p for p in path.split("/") if p]
        svc = self.svc

        if method == "GET" and parts == ["health"]:
            return 200, None  # 由 service.py 的健康负载接管
        if parts[:1] != ["api"]:
            return 404, {"error": "not found"}
        parts = parts[1:]

        if method == "POST" and parts == ["contributors"]:
            return 200, svc.register_contributor(body.get("name"), body.get("contact"))

        if method == "POST" and parts == ["uploads"]:
            return 200, svc.open_upload(
                body.get("contributor_id"), body.get("content_hash", ""), body.get("total_size", 0)
            )
        if len(parts) == 3 and parts[0] == "uploads" and parts[2] == "chunks" and method == "PUT":
            return 200, svc.upload_chunk(
                parts[1], int(body.get("offset", 0)), _b64(body.get("data_b64"), "data_b64")
            )
        if len(parts) == 3 and parts[0] == "uploads" and parts[2] == "complete" and method == "POST":
            return 200, svc.complete_upload(
                parts[1],
                title=body.get("title", ""),
                description=body.get("description", ""),
                media_type=body.get("media_type", ""),
                probe=body.get("probe") or {},
                terms_version=body.get("terms_version"),
                terms_text=body.get("terms_text"),
                license_scope=body.get("license_scope"),
                actor=body.get("actor"),
            )

        if method == "GET" and parts == ["queue", "publishable"]:
            return 200, {"queue": svc.publishable_queue()}
        if method == "GET" and parts == ["public", "works"]:
            return 200, {"works": svc.public_works()}
        if method == "POST" and parts == ["receipts"]:
            return 200, svc.record_receipt(
                channel=body.get("channel", ""),
                publication_key=body.get("publication_key", ""),
                receipt_id=body.get("receipt_id", ""),
                status=body.get("status", ""),
                detail=body.get("detail", ""),
            )

        if len(parts) >= 2 and parts[0] == "works":
            work_id = parts[1]
            action = parts[2:] if len(parts) > 2 else []
            if method == "GET" and not action:
                return 200, svc.get_work(work_id)
            if method == "GET" and action == ["provenance"]:
                return 200, svc.provenance(work_id)
            if method != "POST":
                return 404, {"error": "not found"}
            if action == ["notes"]:
                return 200, svc.add_note(work_id, body.get("actor"), body.get("text", ""))
            if action == ["versions"]:
                return 200, svc.replace_file(
                    work_id,
                    body.get("actor"),
                    _b64(body.get("data_b64"), "data_b64"),
                    note=body.get("note", ""),
                    terms_version=body.get("terms_version"),
                    terms_text=body.get("terms_text"),
                )
            if action == ["disputes"]:
                return 200, svc.open_dispute(
                    work_id, body.get("actor"), body.get("claimant", ""), body.get("detail", "")
                )
            if action == ["disputes", "resolve"]:
                return 200, svc.resolve_dispute(
                    work_id,
                    body.get("actor"),
                    body.get("outcome", ""),
                    confirmed_contributor_id=body.get("confirmed_contributor_id"),
                )
            if action == ["takedown"]:
                return 200, svc.request_takedown(work_id, body.get("actor"), body.get("reason", ""))
            if action == ["takedown", "execute"]:
                return 200, svc.execute_takedown(work_id, body.get("actor"))
            if action == ["select"]:
                return 200, svc.select(work_id, body.get("actor"))
            if action == ["approve"]:
                return 200, svc.approve(work_id, body.get("actor"))
            if action == ["publish"]:
                return 200, {"publications": svc.publish(
                    work_id, body.get("actor"), channels=body.get("channels")
                )}
        return 404, {"error": "not found"}
