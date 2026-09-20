"""事件目录、读模型投影与发布策略。

所有状态都由事件重放得到，服务层只追加事件、不更新状态。
状态名集中定义，避免各模块各写一份字符串。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# ---- 角色（选用 / 复核 / 发布职责分离） -----------------------------------

ROLE_EDITOR = "editor"        # 编辑：选用作品、指定版本与渠道
ROLE_REVIEWER = "reviewer"    # 复核：人工核验结论、发布前复核
ROLE_PUBLISHER = "publisher"  # 发布：投递渠道、登记回执
ROLE_ADMIN = "admin"
STAFF_ROLES = {ROLE_EDITOR, ROLE_REVIEWER, ROLE_PUBLISHER, ROLE_ADMIN}

# ---- 事件类型 -------------------------------------------------------------

SUBMISSION_CREATED = "SubmissionCreated"
NOTE_SUPPLEMENTED = "NoteSupplemented"
FILE_REPLACED = "FileReplaced"
TECH_CHECK_RECORDED = "TechCheckRecorded"
DETECTION_SIGNALED = "DetectionSignaled"
MANUAL_REVIEW_REQUIRED = "ManualReviewRequired"
MANUAL_VERDICT_APPLIED = "ManualVerdictApplied"
RIGHTS_CLAIM_RAISED = "RightsClaimRaised"
RIGHTS_CLAIM_RESOLVED = "RightsClaimResolved"
WITHDRAWAL_REQUESTED = "WithdrawalRequested"
WITHDRAWAL_ACCEPTED = "WithdrawalAccepted"
DISTRIBUTION_HALTED = "DistributionHalted"
VERSION_ENTERED_LAYOUT = "VersionEnteredLayout"

REVIEW_OPENED = "ReviewOpened"
REVIEW_VERDICT_RECORDED = "ReviewVerdictRecorded"
REVIEW_CLOSED = "ReviewClosed"

SELECTION_CREATED = "SelectionCreated"
SELECTION_APPROVED = "SelectionApproved"
SELECTION_REJECTED = "SelectionRejected"
SELECTION_DISPATCHED = "SelectionDispatched"

DELIVERY_CREATED = "DeliveryCreated"
DELIVERY_RESULT_RECORDED = "DeliveryResultRecorded"
DELIVERY_WITHDRAWN = "DeliveryWithdrawn"

# 检测信号类型：只产生线索，永远不直接定性
SIGNAL_SIMILAR = "similar"        # 相似作品（感知哈希）
SIGNAL_REUSE = "reuse"            # 疑似搬运（外部线索/水印等）
SIGNAL_SYNTHETIC = "synthetic"    # 疑似合成

VERDICT_AUTHENTIC = "authentic"
VERDICT_COPIED = "copied"
VERDICT_SYNTHETIC = "synthetic"
VERDICT_INCONCLUSIVE = "inconclusive"

RESULT_SUCCEEDED = "succeeded"
RESULT_FAILED = "failed"
RESULT_WITHDRAWN = "withdrawn"

# 投稿件状态
ST_RECEIVED = "received"
ST_TECH_FAILED = "tech_failed"
ST_MANUAL_REVIEW = "manual_review"
ST_VERIFIED = "verified"
ST_REJECTED = "rejected"
ST_WITHDRAWN = "withdrawn"


@dataclass
class FileVersion:
    sha256: str
    size: int
    filename: str
    reason: str = "initial"


@dataclass
class TechCheck:
    sha256: str
    overall: str  # pass | fail
    checks: list[dict[str, Any]]
    seq: int


@dataclass
class RightsClaim:
    claim_id: str
    claimant: str
    contact: str
    note: str
    open: bool = True
    resolution: str | None = None


@dataclass
class SubmissionView:
    submission_id: str
    campaign_id: str
    display_name: str
    contact: str
    media_type: str
    title: str = ""
    note: str = ""
    terms_version: str = ""
    terms_digest: str = ""
    declared_original: bool = True
    ai_assist: str = "none"
    versions: list[FileVersion] = field(default_factory=list)
    replaces_submission_id: str | None = None
    upload_session_id: str | None = None
    contributor_token: str | None = None
    public_code: str | None = None
    tech_checks: list[TechCheck] = field(default_factory=list)
    signals: list[dict[str, Any]] = field(default_factory=list)
    rights_claims: list[RightsClaim] = field(default_factory=list)
    open_review_ids: list[str] = field(default_factory=list)
    closed_review_ids: list[str] = field(default_factory=list)
    verdict: str | None = None
    withdrawal: dict[str, Any] | None = None
    halted: bool = False
    layout_versions: list[dict[str, Any]] = field(default_factory=list)

    @property
    def current(self) -> FileVersion | None:
        return self.versions[-1] if self.versions else None

    @property
    def latest_check(self) -> TechCheck | None:
        return self.tech_checks[-1] if self.tech_checks else None

    @property
    def open_claims(self) -> list[RightsClaim]:
        return [c for c in self.rights_claims if c.open]

    @property
    def status(self) -> str:
        if self.withdrawal is not None:
            return ST_WITHDRAWN
        if self.verdict in (VERDICT_COPIED, VERDICT_SYNTHETIC):
            return ST_REJECTED
        if self.open_review_ids or self.open_claims or self.verdict == VERDICT_INCONCLUSIVE:
            return ST_MANUAL_REVIEW
        if self.signals and self.verdict is None:
            return ST_MANUAL_REVIEW
        latest = self.latest_check
        if latest is None:
            return ST_RECEIVED
        if latest.overall != "pass":
            return ST_TECH_FAILED
        if self.verdict == VERDICT_AUTHENTIC:
            return ST_VERIFIED
        return ST_RECEIVED

    def version_hashes(self) -> set[str]:
        return {v.sha256 for v in self.versions}


@dataclass
class ReviewView:
    review_id: str
    submission_id: str
    reasons: list[str]
    details: dict[str, Any]
    open: bool = True
    verdict: str | None = None
    reviewer: str | None = None
    note: str | None = None
    events: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class DeliveryView:
    delivery_id: str
    selection_id: str
    submission_id: str
    channel: str
    sha256: str
    terms_version: str
    terms_digest: str
    basis: dict[str, Any]
    state: str = "pending"  # pending | succeeded | failed | withdrawn
    channel_ref: str | None = None
    detail: str | None = None
    result_at: float | None = None
    result_seq: int | None = None
    halted: bool = False
    results: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class SelectionView:
    selection_id: str
    submission_id: str
    sha256: str
    channels: list[str]
    editor: str
    note: str
    approved: bool = False
    rejected: bool = False
    reviewer: str | None = None
    review_note: str | None = None
    deliveries: dict[str, str] = field(default_factory=dict)  # channel -> delivery_id


class Projection:
    """从事件流重放全部读模型。"""

    def __init__(self):
        self.submissions: dict[str, SubmissionView] = {}
        self.reviews: dict[str, ReviewView] = {}
        self.selections: dict[str, SelectionView] = {}
        self.deliveries: dict[str, DeliveryView] = {}

    def apply(self, events: list[dict[str, Any]]) -> None:
        for e in events:
            self._apply_one(e)

    def _apply_one(self, e: dict[str, Any]) -> None:
        t, p, aid = e["event_type"], e["payload"], e["aggregate_id"]
        atype = e["aggregate_type"]
        if atype == "submission":
            self._apply_submission(t, aid, p, e)
        elif atype == "review":
            self._apply_review(t, aid, p, e)
        elif atype == "selection":
            self._apply_selection(t, aid, p)
        elif atype == "delivery":
            self._apply_delivery(t, aid, p, e)

    # ---- 投稿件 --------------------------------------------------------

    def _apply_submission(self, t, sid, p, e):
        if t == SUBMISSION_CREATED:
            s = SubmissionView(
                submission_id=sid,
                campaign_id=p["campaign_id"],
                display_name=p["contributor"]["display_name"],
                contact=p["contributor"]["contact"],
                media_type=p["media_type"],
                title=p.get("title", ""),
                note=p.get("note", ""),
                terms_version=p["terms"]["version"],
                terms_digest=p["terms"]["digest"],
                declared_original=p.get("declared_original", True),
                ai_assist=p.get("ai_assist", "none"),
                replaces_submission_id=p.get("replaces_submission_id"),
                upload_session_id=p.get("upload_session_id"),
                contributor_token=p.get("contributor_token"),
                public_code=p.get("public_code"),
            )
            s.versions.append(FileVersion(p["sha256"], p["size"], p["filename"]))
            self.submissions[sid] = s
            return
        s = self.submissions.get(sid)
        if s is None:
            raise KeyError(f"事件 {t} 指向不存在的投稿件 {sid}")
        if t == NOTE_SUPPLEMENTED:
            s.note = (s.note + "\n" + p["note"]).strip()
        elif t == FILE_REPLACED:
            s.versions.append(FileVersion(p["sha256"], p["size"], p["filename"],
                                          p.get("reason", "replace")))
            # 换了文件，旧检查/信号只作历史，当前版本需重新核验
        elif t == TECH_CHECK_RECORDED:
            s.tech_checks.append(TechCheck(
                p["sha256"], p["overall"], p["checks"], e["seq"]))
        elif t == DETECTION_SIGNALED:
            for sig in p["signals"]:
                s.signals.append({**sig, "sha256": p["sha256"], "seq": e["seq"]})
        elif t == MANUAL_REVIEW_REQUIRED:
            for rid in p["review_ids"]:
                if rid not in s.open_review_ids and rid not in s.closed_review_ids:
                    s.open_review_ids.append(rid)
            # 出现新的未决核验（新证据），旧的自动汇总结论挂起，
            # 等全部核验关闭后由人工结论重新汇总；检测分数本身永不写入结论。
            if any(rid in s.open_review_ids for rid in p["review_ids"]):
                s.verdict = None
        elif t == MANUAL_VERDICT_APPLIED:
            s.verdict = p["verdict"]
        elif t == RIGHTS_CLAIM_RAISED:
            s.rights_claims.append(RightsClaim(
                p["claim_id"], p["claimant"], p.get("contact", ""), p["note"]))
        elif t == RIGHTS_CLAIM_RESOLVED:
            for c in s.rights_claims:
                if c.claim_id == p["claim_id"]:
                    c.open = False
                    c.resolution = p["resolution"]
            if p.get("resolution") == "upheld":
                s.verdict = VERDICT_COPIED
        elif t == WITHDRAWAL_REQUESTED:
            s.withdrawal = {"reason": p["reason"], "requested_by": p.get("requested_by"),
                            "accepted": False}
        elif t == WITHDRAWAL_ACCEPTED:
            if s.withdrawal is None:
                s.withdrawal = {"reason": p.get("reason", ""), "accepted": True}
            else:
                s.withdrawal["accepted"] = True
        elif t == DISTRIBUTION_HALTED:
            s.halted = True
            for did in p.get("delivery_ids", []):
                if did in self.deliveries:
                    self.deliveries[did].halted = True
        elif t == VERSION_ENTERED_LAYOUT:
            s.layout_versions.append(p)

    # ---- 人工核验任务 ---------------------------------------------------

    def _apply_review(self, t, rid, p, e):
        if t == REVIEW_OPENED:
            self.reviews[rid] = ReviewView(
                review_id=rid, submission_id=p["submission_id"],
                reasons=list(p["reasons"]), details=p.get("details", {}))
            self.reviews[rid].events.append(e)
        elif rid in self.reviews:
            rv = self.reviews[rid]
            rv.events.append(e)
            if t == REVIEW_VERDICT_RECORDED:
                rv.verdict = p["verdict"]
                rv.reviewer = p["reviewer"]
                rv.note = p.get("note")
            elif t == REVIEW_CLOSED:
                rv.open = False
                s = self.submissions.get(rv.submission_id)
                if s and rid in s.open_review_ids:
                    s.open_review_ids.remove(rid)
                    s.closed_review_ids.append(rid)

    # ---- 编辑选用 -------------------------------------------------------

    def _apply_selection(self, t, lid, p):
        if t == SELECTION_CREATED:
            self.selections[lid] = SelectionView(
                selection_id=lid, submission_id=p["submission_id"],
                sha256=p["sha256"], channels=list(p["channels"]),
                editor=p["editor"], note=p.get("note", ""))
        elif lid in self.selections:
            sel = self.selections[lid]
            if t == SELECTION_APPROVED:
                sel.approved = True
                sel.reviewer = p["reviewer"]
                sel.review_note = p.get("note")
            elif t == SELECTION_REJECTED:
                sel.rejected = True
                sel.reviewer = p["reviewer"]
                sel.review_note = p.get("reason")
            elif t == SELECTION_DISPATCHED:
                for d in p["deliveries"]:
                    sel.deliveries[d["channel"]] = d["delivery_id"]

    # ---- 渠道投递 -------------------------------------------------------

    def _apply_delivery(self, t, did, p, e):
        if t == DELIVERY_CREATED:
            self.deliveries[did] = DeliveryView(
                delivery_id=did, selection_id=p["selection_id"],
                submission_id=p["submission_id"], channel=p["channel"],
                sha256=p["sha256"], terms_version=p["terms"]["version"],
                terms_digest=p["terms"]["digest"], basis=p.get("basis", {}))
        elif did in self.deliveries:
            d = self.deliveries[did]
            if t == DELIVERY_RESULT_RECORDED:
                # 幂等汇总：撤回终态最高；成功不被迟到的失败覆盖；失败可被重试成功抬升
                incoming = p["result"]
                if not (d.state == RESULT_WITHDRAWN and incoming != RESULT_WITHDRAWN):
                    rank = {"pending": 0, RESULT_FAILED: 1,
                            RESULT_SUCCEEDED: 2, RESULT_WITHDRAWN: 3}
                    if rank.get(incoming, 0) >= rank.get(d.state, 0):
                        d.state = incoming
                d.channel_ref = p.get("channel_ref")
                d.result_at = p.get("at", e["created_at"])
                d.result_seq = e["seq"]
                d.results.append({"callback_id": p.get("callback_id"),
                                  "result": p["result"], "detail": p.get("detail"),
                                  "at": d.result_at})
                if p.get("detail"):
                    d.detail = p["detail"]
            elif t == DELIVERY_WITHDRAWN:
                d.state = RESULT_WITHDRAWN
                d.detail = p.get("detail")
                d.result_at = p.get("at", e["created_at"])


# ---- 发布策略 --------------------------------------------------------------


def selection_delivery_state(sel: SelectionView, proj: Projection) -> dict[str, str]:
    """每个渠道的最终状态；同一回执幂等汇总，重复上报不翻转结论。"""
    out = {}
    for ch in sel.channels:
        did = sel.deliveries.get(ch)
        out[ch] = proj.deliveries[did].state if did else "pending"
    return out


def publishable_selections(proj: Projection) -> list[dict[str, Any]]:
    """可发布队列：已复核通过、作品已人工确认原创、无未决争议/撤稿/停止分发，
    且至少一个渠道尚未成功。失败可重试，撤回与停止分发不再出现。"""
    queue = []
    for sel in proj.selections.values():
        if not sel.approved or sel.rejected:
            continue
        s = proj.submissions.get(sel.submission_id)
        if s is None or s.status != ST_VERIFIED or s.halted:
            continue
        if sel.sha256 not in s.version_hashes():
            continue  # 选用版本已不存在（被替换且未保留）——不予发布
        states = selection_delivery_state(sel, proj)
        pending = [ch for ch, st in states.items()
                   if st in ("pending", "failed")]
        if not pending:
            continue
        queue.append({
            "selection_id": sel.selection_id,
            "submission_id": sel.submission_id,
            "display_name": s.display_name,
            "title": s.title,
            "sha256": sel.sha256,
            "channels_pending": pending,
            "channel_states": states,
            "terms_version": sel_sha256_terms(proj, sel),
        })
    queue.sort(key=lambda x: x["selection_id"])
    return queue


def sel_sha256_terms(proj: Projection, sel: SelectionView) -> str:
    s = proj.submissions[sel.submission_id]
    return s.terms_version


def public_catalog(proj: Projection) -> list[dict[str, Any]]:
    """公开目录：仅含有成功刊发记录的作品，剔除联系方式与未采用素材。"""
    items = []
    for s in proj.submissions.values():
        published = []
        for d in proj.deliveries.values():
            if d.submission_id == s.submission_id and d.state == RESULT_SUCCEEDED:
                published.append({"channel": d.channel, "at": d.result_at,
                                  "channel_ref": d.channel_ref})
        if not published:
            continue
        published.sort(key=lambda x: x["at"] or 0)
        items.append({
            "submission_id": s.submission_id,
            "display_name": s.display_name,
            "title": s.title,
            "media_type": s.media_type,
            "published": published,
        })
    items.sort(key=lambda x: x["submission_id"])
    return items
