"""渠道分发与回执。

* 只有复核通过的选用单可由 publisher 投递，投递按渠道各生成一条记录；
* 渠道回执（成功/失败/撤回）带渠道侧回调编号，**同一回调重复回传只入账一次**，
  不同回调按"撤回 > 成功 > 失败 > 待发"汇总，部分失败可重试且不影响已成功渠道；
* 停止分发后不能再投递；撤回单独成事件，已进入版面的版本保留刊发依据。
"""

from __future__ import annotations

from typing import Any

from .domain import (
    DELIVERY_CREATED, DELIVERY_RESULT_RECORDED, DELIVERY_WITHDRAWN,
    RESULT_FAILED, RESULT_SUCCEEDED, RESULT_WITHDRAWN,
    SELECTION_DISPATCHED,
)
from .eventstore import ConflictError, new_id

_PRECEDENCE = {None: 0, "pending": 0, RESULT_FAILED: 1,
               RESULT_SUCCEEDED: 2, RESULT_WITHDRAWN: 3}
VALID_RESULTS = {RESULT_SUCCEEDED, RESULT_FAILED, RESULT_WITHDRAWN}


class DistributionService:
    def __init__(self, app):
        self.app = app

    def dispatch(self, selection_id: str, publisher: str, role: str) -> list[str]:
        from .domain import ROLE_PUBLISHER, ROLE_ADMIN
        if role not in (ROLE_PUBLISHER, ROLE_ADMIN):
            raise ConflictError("仅发布岗可向渠道投递")
        sel = self.app.proj.selections.get(selection_id)
        if sel is None:
            raise ConflictError("选用单不存在")
        if not sel.approved or sel.rejected:
            raise ConflictError("选用单未经复核通过，不得投递")
        s = self.app.proj.submissions[sel.submission_id]
        if s.halted or s.withdrawal is not None:
            raise ConflictError("作品处于停止分发/撤稿状态，禁止新投递")
        terms = {"version": s.terms_version, "digest": s.terms_digest}
        delivery_ids: list[dict[str, str]] = []
        created: list[str] = []
        with self.app.store.tx() as conn:
            for ch in sel.channels:
                existing_id = sel.deliveries.get(ch)
                existing = (self.app.proj.deliveries.get(existing_id)
                            if existing_id else None)
                if existing is not None:
                    if existing.state == RESULT_WITHDRAWN or \
                            (existing.halted and existing.state != RESULT_SUCCEEDED):
                        raise ConflictError(f"渠道 {ch} 已撤回/停发，不能重复投递")
                    delivery_ids.append({"channel": ch, "delivery_id": existing_id})
                    continue
                did = new_id()
                self.app.store.append(
                    conn, "delivery", did, DELIVERY_CREATED, publisher,
                    {"selection_id": selection_id,
                     "submission_id": sel.submission_id,
                     "channel": ch, "sha256": sel.sha256,
                     "terms": terms,
                     "basis": {
                         "editor": sel.editor,
                         "reviewer": sel.reviewer,
                         "manual_verdict": s.verdict,
                     }})
                delivery_ids.append({"channel": ch, "delivery_id": did})
                created.append(did)
            if created:
                self.app.store.append(
                    conn, "selection", selection_id, SELECTION_DISPATCHED, publisher,
                    {"deliveries": delivery_ids})
        self.app.reload()
        return created

    def record_result(self, delivery_id: str, result: str,
                      callback_id: str, channel_ref: str | None = None,
                      detail: str | None = None) -> dict[str, Any]:
        """登记一条渠道回执。callback_id 相同视为同一回调，幂等返回汇总状态。"""
        if result not in VALID_RESULTS:
            raise ConflictError(f"回执状态非法: {result}")
        if not callback_id:
            raise ConflictError("渠道回调编号必填，用于幂等去重")
        d = self.app.proj.deliveries.get(delivery_id)
        if d is None:
            raise ConflictError("投递记录不存在")
        # 幂等命中：同一回调编号已入账
        if any(r["callback_id"] == callback_id for r in d.results):
                return {"delivery_id": delivery_id, "state": d.state,
                        "deduped": True, "callback_id": callback_id}
        with self.app.store.tx() as conn:
            self.app.store.append(
                conn, "delivery", delivery_id, DELIVERY_RESULT_RECORDED,
                f"channel:{d.channel}",
                {"result": result, "callback_id": callback_id,
                 "channel_ref": channel_ref, "detail": detail})
        self.app.reload()
        return {"delivery_id": delivery_id,
                "state": self.app.proj.deliveries[delivery_id].state,
                "deduped": False, "callback_id": callback_id}

    def withdraw(self, delivery_id: str, publisher: str, role: str,
                 reason: str, callback_id: str | None = None) -> None:
        from .domain import ROLE_PUBLISHER, ROLE_ADMIN
        if role not in (ROLE_PUBLISHER, ROLE_ADMIN):
            raise ConflictError("仅发布岗可登记渠道撤回")
        d = self.app.proj.deliveries.get(delivery_id)
        if d is None:
            raise ConflictError("投递记录不存在")
        if d.state == RESULT_WITHDRAWN:
            return
        with self.app.store.tx() as conn:
            self.app.store.append(
                conn, "delivery", delivery_id, DELIVERY_WITHDRAWN, publisher,
                {"reason": reason,
                 "callback_id": callback_id or f"wd-{new_id()[:12]}"})
        self.app.reload()

    @staticmethod
    def summarize(d) -> dict[str, Any]:
        return {
            "delivery_id": d.delivery_id,
            "channel": d.channel,
            "state": d.state,
            "channel_ref": d.channel_ref,
            "halted": d.halted,
            "callbacks": len(d.results),
            "last_detail": d.detail,
        }
