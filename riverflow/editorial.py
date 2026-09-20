"""编辑选用、人工复核、发布编排。

职责分离（三人三角色，互相不能代办）：
* editor  选用作品并指定版本与渠道；
* reviewer 处理人工核验、权属争议、撤稿受理，并复核选用单；
* publisher 把复核通过的选用单投递到渠道、登记版面。

技术分数永远不出现在定性路径上：similar/reuse/synthetic 只是待核验线索，
最终结论只来自 reviewer 的人工裁决事件。
"""

from __future__ import annotations

from typing import Any

from .domain import (
    DELIVERY_CREATED, DISTRIBUTION_HALTED, FILE_REPLACED,
    MANUAL_VERDICT_APPLIED, RESULT_FAILED, RESULT_SUCCEEDED, RESULT_WITHDRAWN,
    REVIEW_CLOSED, REVIEW_VERDICT_RECORDED, RIGHTS_CLAIM_RESOLVED,
    SELECTION_APPROVED, SELECTION_CREATED, SELECTION_DISPATCHED,
    SELECTION_REJECTED, ST_VERIFIED, VERSION_ENTERED_LAYOUT,
    WITHDRAWAL_ACCEPTED,
)
from .eventstore import ConflictError, new_id

VALID_VERDICTS = {"authentic", "copied", "synthetic", "inconclusive"}


class EditorialService:
    def __init__(self, app):
        self.app = app

    # ---- 人工核验结论 ---------------------------------------------------

    def record_verdict(self, review_id: str, reviewer: str, role: str,
                       verdict: str, note: str = "") -> None:
        from .domain import ROLE_REVIEWER, ROLE_ADMIN
        if role not in (ROLE_REVIEWER, ROLE_ADMIN):
            raise ConflictError("仅复核岗可作出人工核验结论")
        rv = self.app.proj.reviews.get(review_id)
        if rv is None:
            raise ConflictError("核验任务不存在")
        if not rv.open:
            raise ConflictError("核验任务已关闭，结论不可改写（可另开复核）")
        if verdict not in VALID_VERDICTS:
            raise ConflictError(f"结论取值非法: {verdict}")
        sid = rv.submission_id
        with self.app.store.tx() as conn:
            self.app.store.append(
                conn, "review", review_id, REVIEW_VERDICT_RECORDED, reviewer,
                {"verdict": verdict, "reviewer": reviewer, "note": note.strip()})
            self.app.store.append(
                conn, "review", review_id, REVIEW_CLOSED, reviewer,
                {"verdict": verdict})
        self.app.reload()
        self._apply_aggregate_verdict(sid, reviewer)

    def _apply_aggregate_verdict(self, sid: str, reviewer: str) -> None:
        s = self.app.proj.submissions[sid]
        if s.open_review_ids:
            return  # 仍有未决核验，暂不下汇总结论
        verdicts = [self.app.proj.reviews[rid].verdict
                    for rid in s.closed_review_ids]
        verdicts = [v for v in verdicts if v]
        if not verdicts:
            return
        if "copied" in verdicts:
            agg = "copied"
        elif "synthetic" in verdicts:
            agg = "synthetic"
        elif all(v == "authentic" for v in verdicts):
            agg = "authentic"
        else:
            agg = "inconclusive"
        with self.app.store.tx() as conn:
            self.app.store.append(
                conn, "submission", sid, MANUAL_VERDICT_APPLIED, reviewer,
                {"verdict": agg,
                 "from_reviews": list(s.closed_review_ids)})
        self.app.reload()

    # ---- 权属争议 -------------------------------------------------------

    def resolve_rights_claim(self, sid: str, claim_id: str, reviewer: str,
                             role: str, resolution: str, note: str = "") -> None:
        """resolution: upheld（主张成立，非原作/无权授权）| rejected（不成立）。"""
        from .domain import ROLE_REVIEWER, ROLE_ADMIN
        if role not in (ROLE_REVIEWER, ROLE_ADMIN):
            raise ConflictError("仅复核岗可裁决权属争议")
        if resolution not in ("upheld", "rejected"):
            raise ConflictError("争议裁决取值须为 upheld/rejected")
        s = self.app.proj.submissions.get(sid)
        if s is None:
            raise ConflictError("投稿件不存在")
        claim = next((c for c in s.rights_claims if c.claim_id == claim_id), None)
        if claim is None or not claim.open:
            raise ConflictError("主张不存在或已裁决")
        linked = [rid for rid in s.open_review_ids
                  if self.app.proj.reviews[rid].details.get("claim_id") == claim_id]
        verdict = "copied" if resolution == "upheld" else "authentic"
        with self.app.store.tx() as conn:
            self.app.store.append(
                conn, "submission", sid, RIGHTS_CLAIM_RESOLVED, reviewer,
                {"claim_id": claim_id, "resolution": resolution,
                 "note": note.strip()})
            for rid in linked:
                self.app.store.append(
                    conn, "review", rid, REVIEW_VERDICT_RECORDED, reviewer,
                    {"verdict": verdict, "reviewer": reviewer,
                     "note": f"权属主张 {claim_id} 裁决为 {resolution}"})
                self.app.store.append(
                    conn, "review", rid, REVIEW_CLOSED, reviewer,
                    {"verdict": verdict, "claim_id": claim_id})
        self.app.reload()
        self._apply_aggregate_verdict(sid, reviewer)

    # ---- 撤稿：受理即停发，版面依据保留 ----------------------------------

    def accept_withdrawal(self, sid: str, reviewer: str, role: str,
                          note: str = "") -> dict[str, Any]:
        from .domain import ROLE_REVIEWER, ROLE_PUBLISHER, ROLE_ADMIN
        if role not in (ROLE_REVIEWER, ROLE_PUBLISHER, ROLE_ADMIN):
            raise ConflictError("仅复核/发布岗可受理撤稿")
        s = self.app.proj.submissions.get(sid)
        if s is None:
            raise ConflictError("投稿件不存在")
        if s.withdrawal is None:
            raise ConflictError("投稿人尚未提出撤稿申请")
        if s.withdrawal.get("accepted"):
            raise ConflictError("撤稿已受理，事件轨迹可查")
        active_delivery_ids = [
            d.delivery_id for d in self.app.proj.deliveries.values()
            if d.submission_id == sid and d.state not in (RESULT_WITHDRAWN,)
        ]
        with self.app.store.tx() as conn:
            self.app.store.append(
                conn, "submission", sid, WITHDRAWAL_ACCEPTED, reviewer,
                {"reason": s.withdrawal.get("reason", ""), "note": note.strip()})
            self.app.store.append(
                conn, "submission", sid, DISTRIBUTION_HALTED, reviewer,
                {"reason": "withdrawal-accepted",
                 "delivery_ids": active_delivery_ids})
        self.app.reload()
        return {"halted_deliveries": active_delivery_ids,
                "note": "已成功刊发的渠道等待撤回回执；版面依据事件保留不删"}

    # ---- 版面进入：冻结刊发依据 -----------------------------------------

    def enter_layout(self, delivery_id: str, publisher: str, role: str,
                     edition: str, layout_ref: str) -> None:
        from .domain import ROLE_PUBLISHER, ROLE_ADMIN
        if role not in (ROLE_PUBLISHER, ROLE_ADMIN):
            raise ConflictError("仅发布岗可登记版面")
        d = self.app.proj.deliveries.get(delivery_id)
        if d is None:
            raise ConflictError("投递记录不存在")
        s = self.app.proj.submissions[d.submission_id]
        sel = self.app.proj.selections[d.selection_id]
        basis = {
            "delivery_id": delivery_id,
            "selection_id": d.selection_id,
            "selected_by": sel.editor,
            "approved_by": sel.reviewer,
            "terms_version": d.terms_version,
            "terms_digest": d.terms_digest,
            "verdict_at_selection": s.verdict,
            "edition": edition,
            "layout_ref": layout_ref,
            "channel": d.channel,
        }
        with self.app.store.tx() as conn:
            self.app.store.append(
                conn, "submission", d.submission_id, VERSION_ENTERED_LAYOUT,
                publisher, {"sha256": d.sha256, **basis})
        self.app.reload()

    # ---- 编辑选用 -------------------------------------------------------

    def select(self, sid: str, sha256: str, channels: list[str],
               editor: str, role: str, note: str = "") -> str:
        from .domain import ROLE_EDITOR, ROLE_ADMIN
        if role not in (ROLE_EDITOR, ROLE_ADMIN):
            raise ConflictError("仅编辑岗可选用作品")
        s = self.app.proj.submissions.get(sid)
        if s is None:
            raise ConflictError("投稿件不存在")
        self._ensure_publishable_state(s)
        if sha256 not in s.version_hashes():
            raise ConflictError("指定版本不属于该投稿件")
        if not channels or any(c not in self.app.config.channels for c in channels):
            raise ConflictError(f"渠道必须是活动配置内的非商业渠道: "
                                f"{self.app.config.channels}")
        if len(set(channels)) != len(channels):
            raise ConflictError("渠道重复")
        lid = new_id()
        with self.app.store.tx() as conn:
            self.app.store.append(
                conn, "selection", lid, SELECTION_CREATED, editor,
                {"submission_id": sid, "sha256": sha256,
                 "channels": channels, "editor": editor, "note": note.strip()})
        self.app.reload()
        return lid

    def approve_selection(self, selection_id: str, reviewer: str,
                          role: str, note: str = "") -> None:
        from .domain import ROLE_REVIEWER, ROLE_ADMIN
        if role not in (ROLE_REVIEWER, ROLE_ADMIN):
            raise ConflictError("仅复核岗可复核选用单")
        sel = self.app.proj.selections.get(selection_id)
        if sel is None:
            raise ConflictError("选用单不存在")
        if sel.approved or sel.rejected:
            raise ConflictError("选用单已结束复核")
        if reviewer == sel.editor and role != ROLE_ADMIN:
            raise ConflictError("选用与复核必须由不同人员完成")
        s = self.app.proj.submissions[sel.submission_id]
        self._ensure_publishable_state(s)
        with self.app.store.tx() as conn:
            self.app.store.append(
                conn, "selection", selection_id, SELECTION_APPROVED, reviewer,
                {"reviewer": reviewer, "note": note.strip()})
        self.app.reload()

    def reject_selection(self, selection_id: str, reviewer: str,
                         role: str, reason: str) -> None:
        from .domain import ROLE_REVIEWER, ROLE_ADMIN
        if role not in (ROLE_REVIEWER, ROLE_ADMIN):
            raise ConflictError("仅复核岗可驳回选用单")
        sel = self.app.proj.selections.get(selection_id)
        if sel is None or sel.approved or sel.rejected:
            raise ConflictError("选用单不存在或已结束复核")
        with self.app.store.tx() as conn:
            self.app.store.append(
                conn, "selection", selection_id, SELECTION_REJECTED, reviewer,
                {"reviewer": reviewer, "reason": reason.strip()})
        self.app.reload()

    @staticmethod
    def _ensure_publishable_state(s) -> None:
        if s.status != ST_VERIFIED:
            raise ConflictError(f"作品当前状态 {s.status}，不满足选用条件")
        if s.halted:
            raise ConflictError("作品已被要求停止分发")
        if s.withdrawal is not None:
            raise ConflictError("作品存在撤稿申请/已撤稿")
        if s.open_claims:
            raise ConflictError("存在未决权属争议")
