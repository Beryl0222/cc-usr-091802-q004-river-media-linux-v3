"""投稿与续传领域服务。

关键不变量：
* 续传以分片主键去重，complete/finalize 幂等——同一上传会话不会生成两件作品；
* 同一投稿人 + 同一内容哈希的在役投稿件自动去重（再次 finalize 返回旧件）；
* 投稿事件冻结提交时条款（版本号 + 全文 SHA256）；
* 技术检查与检测信号在投稿后立即生成，但信号只开启*人工核验*，
  任何分数都不会直接产生"搬运/合成"结论。
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

from . import media
from .domain import (
    DETECTION_SIGNALED, FILE_REPLACED, MANUAL_REVIEW_REQUIRED,
    NOTE_SUPPLEMENTED, REVIEW_OPENED, RIGHTS_CLAIM_RAISED,
    SIGNAL_SIMILAR, SIGNAL_SYNTHETIC, SIGNAL_REUSE,
    SUBMISSION_CREATED, TECH_CHECK_RECORDED, ST_VERIFIED,
)
from .eventstore import ConflictError, new_id

VALID_SIGNALS = {SIGNAL_SIMILAR, SIGNAL_REUSE, SIGNAL_SYNTHETIC}


class UploadService:
    def __init__(self, app):
        self.app = app

    def init_upload(self, uploader: str, media_type: str, filename: str,
                    total_chunks: int, chunk_size: int,
                    declared_sha256: str | None = None,
                    media_meta: dict[str, Any] | None = None) -> dict[str, Any]:
        if media_type not in self.app.config.accepted_media:
            raise ConflictError(f"不接受的素材类型: {media_type}")
        if total_chunks <= 0 or chunk_size <= 0:
            raise ConflictError("分片参数非法")
        sid = self.app.store.create_session(
            uploader, media_type, filename, total_chunks, chunk_size, declared_sha256)
        if media_meta:
            import json as _json
            d = self.app.blobs.root / "chunks" / sid
            d.mkdir(parents=True, exist_ok=True)
            (d / "session-meta.json").write_text(
                _json.dumps(media_meta, ensure_ascii=False), encoding="utf-8")
        return {"session_id": sid, "received_chunks": []}

    def session_meta(self, session_id: str) -> dict[str, Any] | None:
        path = self.app.blobs.root / "chunks" / session_id / "session-meta.json"
        if path.is_file():
            import json as _json
            return _json.loads(path.read_text(encoding="utf-8"))
        return None

    def chunk_path(self, session_id: str, index: int) -> Path:
        d = self.app.blobs.root / "chunks" / session_id
        d.mkdir(parents=True, exist_ok=True)
        return d / f"{index:08d}.part"

    def status(self, session_id: str) -> dict[str, Any]:
        row = self.app.store.get_session(session_id)
        if row is None:
            raise ConflictError("上传会话不存在")
        received = sorted(self.app.store.received_chunks(session_id))
        return {
            "session_id": session_id,
            "total_chunks": row["total_chunks"],
            "received_chunks": received,
            "missing_chunks": [i for i in range(row["total_chunks"]) if i not in received],
            "finalized_submission_id": row["finalized_submission_id"],
        }

    def put_chunk(self, session_id: str, index: int, data: bytes) -> dict[str, Any]:
        row = self.app.store.get_session(session_id)
        if row is None:
            raise ConflictError("上传会话不存在")
        if not 0 <= index < row["total_chunks"]:
            raise ConflictError("分片序号越界")
        path = self.chunk_path(session_id, index)
        with self.app.store.tx() as conn:
            is_new = self.app.store.mark_chunk(conn, session_id, index)
        if is_new:
            path.write_bytes(data)
        st = self.status(session_id)
        st["stored"] = is_new  # False 表示该分片此前已收到（断点续传重发）
        return st

    def finalize(self, session_id: str, contributor: dict[str, str],
                 terms_version: str, title: str = "", note: str = "",
                 declared_original: bool = True, ai_assist: str = "none",
                 replaces_submission_id: str | None = None) -> dict[str, Any]:
        row = self.app.store.get_session(session_id)
        if row is None:
            raise ConflictError("上传会话不存在")
        # 幂等：同一续传会话重复 complete 直接返回已生成的作品
        if row["finalized_submission_id"]:
            return {"submission_id": row["finalized_submission_id"], "reused": True,
                    "reason": "session-already-finalized"}
        missing = [i for i in range(row["total_chunks"])
                   if i not in self.app.store.received_chunks(session_id)]
        if missing:
            raise ConflictError(f"仍有 {len(missing)} 个分片未到齐: {missing[:10]}")
        if terms_version != self.app.config.terms_version:
            raise ConflictError(
                f"条款版本 {terms_version} 已停用，当前为 {self.app.config.terms_version}")
        contact = (contributor.get("contact") or "").strip()
        display_name = (contributor.get("display_name") or "").strip()
        if not contact or not display_name:
            raise ConflictError("缺少署名或联系方式")

        assembled = self._assemble(session_id, row)
        declared = row["declared_sha256"]
        from .blobstore import _hash_file  # 复用哈希逻辑
        digest, size = _hash_file(assembled)
        if declared and declared != digest:
            assembled.unlink(missing_ok=True)
            raise ConflictError("内容哈希与续传前声明不一致，拒绝生成作品")
        target = self.app.blobs.object_path(digest)
        if not target.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            assembled.rename(target)
        else:
            assembled.unlink(missing_ok=True)
        self._write_sidecar(session_id, target)

        # 同人同内容在役作品去重，避免重复生成
        existing = self.app.find_active_submission(contact, digest)
        if existing is not None:
            with self.app.store.tx() as conn:
                self.app.store.attach_finalized(conn, session_id, existing)
            return {"submission_id": existing, "reused": True,
                    "reason": "identical-content-already-submitted"}

        sid = new_id()
        token = uuid.uuid4().hex
        public_code = uuid.uuid4().hex[:10]
        terms = {"version": self.app.config.terms_version,
                 "digest": self.app.config.terms_digest}
        with self.app.store.tx() as conn:
            self.app.store.append(
                conn, "submission", sid, SUBMISSION_CREATED, f"contributor:{contact}",
                {
                    "campaign_id": self.app.config.campaign_id,
                    "media_type": row["media_type"],
                    "filename": row["filename"],
                    "sha256": digest, "size": size,
                    "title": title, "note": note,
                    "contributor": {"display_name": display_name, "contact": contact},
                    "contributor_token": token,
                    "public_code": public_code,
                    "terms": terms,
                    "declared_original": declared_original,
                    "ai_assist": ai_assist,
                    "replaces_submission_id": replaces_submission_id,
                    "upload_session_id": session_id,
                })
            self.app.store.attach_finalized(conn, session_id, sid)
        self.app.reload()
        self.app.tech.run_checks(sid, digest)
        # 技术不合格件不占用人工核验：退回投稿人补正（可替换文件重新进件）
        if self.app.proj.submissions[sid].latest_check.overall == "pass":
            self.app.detection.on_new_version(sid, digest)
        self.app.reload()
        return {"submission_id": sid, "reused": False,
                "contributor_token": token, "public_code": public_code,
                "sha256": digest}

    def _assemble(self, session_id: str, row) -> Path:
        tmp = self.app.blobs.tmp / f"finalize-{session_id}"
        with tmp.open("wb") as out:
            for i in range(row["total_chunks"]):
                out.write(self.chunk_path(session_id, i).read_bytes())
        return tmp

    def _write_sidecar(self, session_id: str, target: Path) -> None:
        """把会话登记的视频元数据写成对象侧车，供分辨率/时长复核。"""
        meta = self.session_meta(session_id)
        if meta:
            import json as _json
            Path(str(target) + ".media.json").write_text(
                _json.dumps(meta, ensure_ascii=False), encoding="utf-8")

    # ---- 投稿后事件：补说明 / 换文件 / 争议 / 撤稿 ----------------------

    def _require_contributor(self, sid: str, token: str):
        s = self.app.proj.submissions[sid]
        if token != s.contributor_token:
            raise ConflictError("投稿人凭证不匹配")
        return s

    def supplement_note(self, sid: str, token: str, note: str) -> None:
        s = self._require_contributor(sid, token)
        if not note.strip():
            raise ConflictError("补充说明为空")
        with self.app.store.tx() as conn:
            self.app.store.append(
                conn, "submission", sid, NOTE_SUPPLEMENTED,
                f"contributor:{s.contact}", {"note": note.strip()})
        self.app.reload()

    def replace_from_session(self, sid: str, token: str, session_id: str,
                             reason: str) -> dict[str, Any]:
        """用新的续传会话替换当前文件（同图改裁剪走这里，形成新版本）。"""
        s = self._require_contributor(sid, token)
        row = self.app.store.get_session(session_id)
        if row is None:
            raise ConflictError("上传会话不存在")
        if row["finalized_submission_id"]:
            raise ConflictError("该会话已用于生成作品，不能作为替换件")
        if row["media_type"] != s.media_type:
            raise ConflictError("替换件类型必须与原件一致")
        missing = [i for i in range(row["total_chunks"])
                   if i not in self.app.store.received_chunks(session_id)]
        if missing:
            raise ConflictError("替换件分片未到齐")
        assembled = self._assemble(session_id, row)
        from .blobstore import _hash_file
        digest, size = _hash_file(assembled)
        if row["declared_sha256"] and row["declared_sha256"] != digest:
            assembled.unlink(missing_ok=True)
            raise ConflictError("替换件哈希与声明不一致")
        if digest in s.version_hashes():
            assembled.unlink(missing_ok=True)
            raise ConflictError("替换件与某个历史版本完全相同，无需替换")
        target = self.app.blobs.object_path(digest)
        if not target.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            assembled.rename(target)
        else:
            assembled.unlink(missing_ok=True)
        self._write_sidecar(session_id, target)
        with self.app.store.tx() as conn:
            self.app.store.append(
                conn, "submission", sid, FILE_REPLACED,
                f"contributor:{s.contact}",
                {"sha256": digest, "size": size, "filename": row["filename"],
                 "reason": reason or "contributor-replace"})
            self.app.store.attach_finalized(conn, session_id, f"replaces:{sid}")
        self.app.reload()
        self.app.tech.run_checks(sid, digest)
        if self.app.proj.submissions[sid].latest_check.overall == "pass":
            self.app.detection.on_new_version(sid, digest)
        self.app.reload()
        return {"submission_id": sid, "sha256": digest}

    def raise_rights_claim(self, sid: str, claimant: str, contact: str,
                           note: str) -> str:
        """任何一方都可提出权属主张；主张立即形成事件并开启人工核验，
        争议期间作品不可刊发。多人主张各自成事件、互不覆盖。"""
        if sid not in self.app.proj.submissions:
            raise ConflictError("投稿件不存在")
        if not claimant.strip() or not note.strip():
            raise ConflictError("主张人与主张理由必填")
        claim_id = new_id()
        with self.app.store.tx() as conn:
            self.app.store.append(
                conn, "submission", sid, RIGHTS_CLAIM_RAISED,
                f"claimant:{claimant}",
                {"claim_id": claim_id, "claimant": claimant,
                 "contact": contact, "note": note.strip()})
            # 每条主张一个独立核验任务（理由含 claim_id，互不吞并），
            # 任一主张未裁决都会挡住刊发
            rid = self.app.detection.open_review(
                conn, sid, ["rights-claim", f"claim:{claim_id}"],
                {"claim_id": claim_id, "claimant": claimant, "note": note.strip()},
                actor=f"claimant:{claimant}")
        self.app.reload()
        return claim_id

    def request_withdrawal(self, sid: str, token: str, reason: str) -> None:
        from .domain import WITHDRAWAL_REQUESTED
        s = self._require_contributor(sid, token)
        if s.withdrawal is not None:
            raise ConflictError("撤稿申请已存在，处理轨迹可查")
        with self.app.store.tx() as conn:
            self.app.store.append(
                conn, "submission", sid, WITHDRAWAL_REQUESTED,
                f"contributor:{s.contact}",
                {"reason": reason.strip(), "requested_by": s.contact})
        self.app.reload()


class DetectionService:
    """相似/搬运/合成信号。信号是线索，不是结论。"""

    def __init__(self, app):
        self.app = app
        self.fingerprints: dict[str, int] = {}  # sha256 -> dhash

    def fingerprint(self, digest: str) -> int | None:
        if digest in self.fingerprints:
            return self.fingerprints[digest]
        path = self.app.blobs.object_path(digest)
        fp = media.dhash_fingerprint(path) if path.is_file() else None
        if fp is not None:
            self.fingerprints[digest] = fp
        return fp

    def on_new_version(self, sid: str, digest: str) -> None:
        """新版本到达：信号 + 常规人工核验（原创性/主题）。

        无论有无风险信号，原创性与主题都必须经人工确认；信号只是追加原因，
        最终结论永远来自核验岗。
        """
        s = self.app.proj.submissions[sid]
        signals: list[dict[str, Any]] = []
        signal_kinds: list[str] = []
        details: dict[str, Any] = {}
        fp = self.fingerprint(digest)
        if s.media_type == "image":
            if fp is None:
                signal_kinds.append("fingerprint-unavailable")
                details["fingerprint-unavailable"] = {
                    "reason": "图像无法解码为感知指纹，相似性无法自动排查，需人工目检"}
            else:
                best = self._nearest(sid, digest, fp)
                if best:
                    other_sid, other_digest, dist = best
                    signals.append({
                        "kind": SIGNAL_SIMILAR,
                        "score": round(1 - dist / 64, 4),
                        "evidence": {
                            "compared_submission_id": other_sid,
                            "compared_sha256": other_digest,
                            "hamming_distance": dist,
                            "same_submission": other_sid == sid,
                            "threshold": media.DHASH_REVIEW_DISTANCE,
                            "method": "dhash64"},
                        "auto_conclusion": False,
                    })
                    signal_kinds.append(SIGNAL_SIMILAR)
                    details[SIGNAL_SIMILAR] = signals[-1]["evidence"]
        reasons = ["originality-confirmation", "theme-fit", *signal_kinds]
        with self.app.store.tx() as conn:
            if signals:
                self.app.store.append(
                    conn, "submission", sid, DETECTION_SIGNALED, "system:detection",
                    {"sha256": digest, "signals": signals})
            self.open_review(conn, sid, reasons, details, actor="system:detection")

    def report_external(self, sid: str, kind: str, score: float | None,
                        evidence: dict[str, Any], reporter: str,
                        sha256: str | None = None) -> str:
        """录入外部检测线索（疑似搬运/疑似合成/相似举报）。

        无论分数高低一律只进入人工核验；score 仅作为证据保存。
        """
        if kind not in VALID_SIGNALS:
            raise ConflictError(f"未知信号类型: {kind}")
        s = self.app.proj.submissions[sid]
        digest = sha256 or (s.current.sha256 if s.current else None)
        signal = {
            "kind": kind,
            "score": score,
            "evidence": evidence or {},
            "reporter": reporter,
            "auto_conclusion": False,
        }
        with self.app.store.tx() as conn:
            self.app.store.append(
                conn, "submission", sid, DETECTION_SIGNALED, reporter,
                {"sha256": digest, "signals": [signal]})
            rid = self.open_review(conn, sid, [kind], {kind: signal}, actor=reporter)
        self.app.reload()
        return rid

    def open_review(self, conn, sid: str, reasons: list[str],
                    details: dict[str, Any], actor: str) -> str:
        """开启人工核验（若已有同类未决核验则挂到既有任务，不淹没旧线索）。"""
        s = self.app.proj.submissions.get(sid)
        if s is not None:
            for rid in s.open_review_ids:
                rv = self.app.proj.reviews[rid]
                if all(r in rv.reasons for r in reasons):
                    return rid
        rid = new_id()
        self.app.store.append(conn, "review", rid, REVIEW_OPENED, actor,
                              {"submission_id": sid, "reasons": reasons,
                               "details": details})
        self.app.store.append(
            conn, "submission", sid, MANUAL_REVIEW_REQUIRED, actor,
            {"sha256": s.current.sha256 if s and s.current else None,
             "review_ids": [rid], "reasons": reasons})
        return rid

    def _nearest(self, sid: str, digest: str, fp: int):
        """在全部在役版本中找最近邻；包含本投稿件的历史版本（同图改裁剪
        也要过人工核验，证据里标明 same_submission）。"""
        best = None
        for other_sid, s in self.app.proj.submissions.items():
            for v in s.versions:
                if other_sid == sid and v.sha256 == digest:
                    continue
                other_fp = self.fingerprints.get(v.sha256)
                if other_fp is None:
                    continue
                dist = media.hamming_distance(fp, other_fp)
                if dist <= media.DHASH_REVIEW_DISTANCE:
                    if best is None or dist < best[2]:
                        best = (other_sid, v.sha256, dist)
        return best
