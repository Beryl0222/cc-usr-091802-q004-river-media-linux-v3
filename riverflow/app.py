"""应用门面：装配事件库、哈希存储与领域服务，提供卷宗/队列/脱敏查询。

卷宗（dossier）逐件回答四个问题：作品来源（谁投、哪次续传、哪些版本）、
授权版本（提交时条款版本与摘要）、处理轨迹（事件流：检查→信号→人工核验
→选用→复核→版面）与传播去向（各渠道状态与回执）。
"""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Any

from .blobstore import BlobStore
from .config import CampaignConfig
from .distribution import DistributionService
from .domain import (
    Projection, RESULT_SUCCEEDED, RESULT_WITHDRAWN, ST_VERIFIED,
    public_catalog, publishable_selections, selection_delivery_state,
)
from .editorial import EditorialService
from .eventstore import EventStore
from .submissions import DetectionService, UploadService
from .tech import TechService

# 公开/投稿人视图中永远不允许出现的字段
SECRET_KEYS = {"contact", "contributor_token"}


class Application:
    def __init__(self, config: CampaignConfig, db_path: str | Path,
                 blob_root: str | Path):
        self.config = config
        self.store = EventStore(db_path)
        self.blobs = BlobStore(blob_root)
        self.proj = Projection()
        self.upload = UploadService(self)
        self.tech = TechService(self)
        self.detection = DetectionService(self)
        self.editorial = EditorialService(self)
        self.distribution = DistributionService(self)
        self.reload()

    def close(self):
        self.store.close()

    def reload(self) -> None:
        self.proj = Projection()
        self.proj.apply(self.store.events())

    # ---- 查询辅助 -------------------------------------------------------

    def find_active_submission(self, contact: str, digest: str) -> str | None:
        """同投稿人 + 同内容哈希，且未撤稿/未被定性为搬运的在役作品。"""
        for sid, s in self.proj.submissions.items():
            if s.contact != contact or digest not in s.version_hashes():
                continue
            if s.withdrawal is not None or s.verdict in ("copied", "synthetic"):
                continue
            return sid
        return None

    def terms_snapshot(self) -> dict[str, str]:
        return {"version": self.config.terms_version,
                "digest": self.config.terms_digest,
                "text": self.config.terms_text}

    # ---- 脱敏视图 -------------------------------------------------------

    def public_status(self, public_code: str) -> dict[str, Any] | None:
        """投稿人凭公开编号自查：不含联系方式；未采用素材不显示处理细节。"""
        sid = next((sid for sid, s in self.proj.submissions.items()
                    if s.public_code == public_code), None)
        if sid is None:
            return None
        s = self.proj.submissions[sid]
        adopted = any(d.submission_id == sid and d.state == RESULT_SUCCEEDED
                      for d in self.proj.deliveries.values())
        return {
            "submission_id": sid,
            "public_code": public_code,
            "title": s.title,
            "media_type": s.media_type,
            "sha256": s.current.sha256 if s.current else None,
            "status": s.status,
            "adopted": adopted,
            "published": [
                {"channel": d.channel, "channel_ref": d.channel_ref}
                for d in self.proj.deliveries.values()
                if d.submission_id == sid and d.state == RESULT_SUCCEEDED
            ],
        }

    def public_catalog(self) -> list[dict[str, Any]]:
        return public_catalog(self.proj)

    # ---- 员工视图（鉴权在 HTTP 层完成） ---------------------------------

    def inbox(self) -> list[dict[str, Any]]:
        rows = []
        for sid, s in self.proj.submissions.items():
            rows.append({
                "submission_id": sid,
                "display_name": s.display_name,
                "contact": s.contact,
                "title": s.title,
                "media_type": s.media_type,
                "status": s.status,
                "current_sha256": s.current.sha256 if s.current else None,
                "open_reviews": s.open_review_ids,
                "open_claims": [c.claim_id for c in s.open_claims],
                "withdrawal": s.withdrawal,
                "halted": s.halted,
            })
        rows.sort(key=lambda r: r["submission_id"])
        return rows

    def reviews_open(self) -> list[dict[str, Any]]:
        return [
            {"review_id": rid, "submission_id": rv.submission_id,
             "reasons": rv.reasons, "details": rv.details}
            for rid, rv in self.proj.reviews.items() if rv.open
        ]

    def publishable_queue(self) -> list[dict[str, Any]]:
        return publishable_selections(self.proj)

    # ---- 卷宗 -----------------------------------------------------------

    def dossier(self, sid: str) -> dict[str, Any]:
        s = self.proj.submissions.get(sid)
        if s is None:
            raise KeyError(sid)
        history = self.store.events("submission", sid)
        related_reviews = {rid: self.proj.reviews[rid]
                           for rid in s.open_review_ids + s.closed_review_ids
                           if rid in self.proj.reviews}
        selections = [sel for sel in self.proj.selections.values()
                      if sel.submission_id == sid]
        deliveries = []
        for sel in selections:
            for ch, did in sel.deliveries.items():
                d = self.proj.deliveries[did]
                deliveries.append({
                    "delivery_id": did,
                    "selection_id": sel.selection_id,
                    "channel": ch,
                    "state": d.state,
                    "channel_ref": d.channel_ref,
                    "halted": d.halted,
                    "results": d.results,
                    "terms_version": d.terms_version,
                    "terms_digest": d.terms_digest,
                    "basis": d.basis,
                })
        return {
            "provenance": {
                "submission_id": sid,
                "campaign_id": s.campaign_id,
                "display_name": s.display_name,
                "contact": s.contact,
                "upload_session_id": s.upload_session_id,
                "replaces_submission_id": s.replaces_submission_id,
                "media_type": s.media_type,
                "title": s.title,
                "note": s.note,
                "declared_original": s.declared_original,
                "ai_assist": s.ai_assist,
            },
            "authorization": {
                "terms_version": s.terms_version,
                "terms_digest": s.terms_digest,
                "non_commercial_only": True,
                "accepted_at_submission": True,
            },
            "files": {
                "current_sha256": s.current.sha256 if s.current else None,
                "versions": [asdict(v) for v in s.versions],
                "derivatives": {
                    v.sha256: self.blobs.derivatives(v.sha256)
                    for v in s.versions
                },
            },
            "tech_checks": [asdict(c) for c in s.tech_checks],
            "signals": s.signals,
            "manual_verdict": s.verdict,
            "reviews": [
                {"review_id": rid,
                 "reasons": rv.reasons,
                 "open": rv.open,
                 "verdict": rv.verdict,
                 "reviewer": rv.reviewer,
                 "note": rv.note,
                 "details": rv.details}
                for rid, rv in sorted(related_reviews.items())
            ],
            "rights_claims": [asdict(c) for c in s.rights_claims],
            "withdrawal": s.withdrawal,
            "halted": s.halted,
            "layout": s.layout_versions,
            "selections": [
                {"selection_id": sel.selection_id,
                 "editor": sel.editor, "reviewer": sel.reviewer,
                 "approved": sel.approved, "rejected": sel.rejected,
                 "sha256": sel.sha256,
                 "channel_states": selection_delivery_state(sel, self.proj)}
                for sel in selections
            ],
            "distributions": deliveries,
            "timeline": [
                {"seq": e["seq"], "event_type": e["event_type"],
                 "actor": e["actor"], "at": e["created_at"],
                 "payload": _redact(e["payload"])}
                for e in history
            ],
        }


def _redact(payload: dict[str, Any]) -> dict[str, Any]:
    out = dict(payload)
    if "contributor" in out and isinstance(out["contributor"], dict):
        out["contributor"] = {k: v for k, v in out["contributor"].items()
                              if k not in SECRET_KEYS}
    out.pop("contributor_token", None)
    return out
