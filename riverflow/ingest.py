"""存量记录导入：把邮箱时代的四类历史记录并入事件流。

支持的记录类型：
* ``legacy_upload``    大文件分片续传（按分片重组、内容哈希入存储，不重复建作品）；
* ``revision``         同图改裁剪——作为同一作品的 FileReplaced 新版本；
* ``claim``            多人主张原作——各自成权属事件并进入人工核验；
* ``manual_review``    历史人工结论（只有人工结论能定性，导入也不例外）；
* ``selection``/``channel_results`` 历史选用与渠道回执（部分失败幂等汇总）；
* ``layout``/``withdrawal`` 历史版面进入与撤稿（保留刊发依据、停止后续分发）。

每条记录以 ``legacy_id`` 幂等：重复导入直接跳过，不产生重复作品/回执。
文件由清单中的生成器描述物化（图片/视频占位 + 切片），模拟邮箱导出包。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from . import demo
from .blobstore import _hash_file
from .domain import (
    FILE_REPLACED, RESULT_FAILED, RESULT_SUCCEEDED, RESULT_WITHDRAWN,
    SUBMISSION_CREATED, WITHDRAWAL_REQUESTED,
)
from .eventstore import ConflictError, new_id

INGEST_SCOPE = "ingest"


class IngestService:
    def __init__(self, app):
        self.app = app
        self.files: dict[str, Path] = {}
        self.submission_by_legacy: dict[str, str] = {}
        self.selection_by_legacy: dict[str, str] = {}

    # ---- 物化历史文件 ----------------------------------------------------

    def materialize(self, manifest: dict[str, Any], base_dir: str | Path) -> None:
        base = Path(base_dir)
        base.mkdir(parents=True, exist_ok=True)
        for f in manifest.get("files", []):
            fid = f["id"]
            spec = f["generator"]
            fdir = base / fid
            fdir.mkdir(exist_ok=True)
            if spec["type"] == "png":
                full = fdir / "original.png"
                demo.write_png(full, spec["width"], spec["height"],
                               demo.river_photo(spec["width"], spec["height"],
                                                spec["seed"]))
            elif spec["type"] == "png_crop":
                # 同一底图的另一种裁剪：在原图坐标空间错位采样，
                # 尺寸不同 => 哈希不同，画面同源 => dHash 相近
                full = fdir / "crop.png"
                sx0 = int(spec.get("shift_x", 40))
                sy0 = int(spec.get("shift_y", 30))
                src_w = int(spec.get("source_width", spec["width"] + sx0))
                src_h = int(spec.get("source_height", spec["height"] + sy0))
                source_px = demo.river_photo(src_w, src_h, spec["seed"])

                def cropped(x, y, _fn=source_px, _sx=sx0, _sy=sy0):
                    return _fn(x + _sx, y + _sy)

                def crop_row(y, _fn=source_px, _sx=sx0, _sy=sy0):
                    # 直接从源行字节切片，裁剪图生成与源图同量级
                    row = _fn.row_bytes(y + _sy)
                    return row[_sx * 3:(_sx + spec["width"]) * 3]

                cropped.row_bytes = crop_row
                demo.write_png(full, spec["width"], spec["height"], cropped)
            elif spec["type"] == "video":
                full = fdir / "video.mp4"
                demo.write_video_stub(full, spec["width"], spec["height"],
                                      spec["duration_seconds"])
            else:
                raise ConflictError(f"未知文件生成器: {spec['type']}")
            n = int(f.get("chunks", 1))
            cdir = fdir / "chunks"
            if not cdir.exists() and n > 1:
                cdir.mkdir()
                data = full.read_bytes()
                size = (len(data) + n - 1) // n
                for i in range(n):
                    (cdir / f"{i}.part").write_bytes(data[i * size:(i + 1) * size])
            self.files[fid] = full

    # ---- 导入主流程 ------------------------------------------------------

    def ingest(self, manifest: dict[str, Any]) -> dict[str, Any]:
        report: list[dict[str, Any]] = []
        for rec in manifest["records"]:
            lid = rec["legacy_id"]
            if self.app.store.idem_get(INGEST_SCOPE, lid) is not None:
                report.append({"legacy_id": lid, "skipped": "already-ingested"})
                continue
            handler = {
                "legacy_upload": self._ingest_upload,
                "revision": self._ingest_revision,
                "claim": self._ingest_claim,
                "manual_review": self._ingest_manual_review,
                "selection": self._ingest_selection,
                "channel_results": self._ingest_channel_results,
                "layout": self._ingest_layout,
                "withdrawal": self._ingest_withdrawal,
            }[rec["type"]]
            result = handler(rec)
            with self.app.store.tx() as conn:
                self.app.store.idem_put(conn, INGEST_SCOPE, lid,
                                        {"result": result})
            report.append({"legacy_id": lid, "type": rec["type"], **result})
        self.app.reload()
        return {"imported": report,
                "publishable_queue": self.app.publishable_queue()}

    def _done(self, **kw) -> dict[str, Any]:
        return kw

    # ---- 大文件续传 ------------------------------------------------------

    def _ingest_upload(self, rec: dict[str, Any]) -> dict[str, Any]:
        f = self.files[rec["file"]]
        media_type = rec["media_type"]
        n = int(rec.get("chunks", 1))
        sess = self.app.upload.init_upload(
            uploader=rec["contributor"]["contact"],
            media_type=media_type, filename=f.name,
            total_chunks=n, chunk_size=max(1, f.stat().st_size // max(n, 1)),
            declared_sha256=rec.get("declared_sha256"))
        sid_session = sess["session_id"]
        cdir = f.parent / "chunks"
        for i in range(n):
            part = cdir / f"{i}.part"
            data = part.read_bytes() if part.exists() else \
                self._slice(f, i, n)
            self.app.upload.put_chunk(sid_session, i, data)
        row = self.app.store.get_session(sid_session)
        assembled = self.app.upload._assemble(sid_session, row)
        digest, size = _hash_file(assembled)
        target = self.app.blobs.object_path(digest)
        if not target.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            assembled.rename(target)
        else:
            assembled.unlink(missing_ok=True)
        self._carry_sidecar(f, target)
        existing = self.app.find_active_submission(
            rec["contributor"]["contact"], digest)
        if existing:
            self.submission_by_legacy[rec["legacy_id"]] = existing
            with self.app.store.tx() as conn:
                self.app.store.attach_finalized(conn, sid_session, existing)
            return self._done(submission_id=existing, reused=True)
        sid = new_id()
        terms = rec.get("terms") or {
            "version": self.app.config.terms_version,
            "digest": self.app.config.terms_digest}
        with self.app.store.tx() as conn:
            self.app.store.append(
                conn, "submission", sid, SUBMISSION_CREATED,
                f"legacy-import:{rec['contributor']['contact']}",
                {"campaign_id": self.app.config.campaign_id,
                 "media_type": media_type, "filename": f.name,
                 "sha256": digest, "size": size,
                 "title": rec.get("title", ""), "note": rec.get("note", ""),
                 "contributor": dict(rec["contributor"]),
                 "contributor_token": rec.get("token", new_id()),
                 "public_code": rec.get("public_code", new_id()[:10]),
                 "terms": terms,
                 "declared_original": rec.get("declared_original", True),
                 "ai_assist": rec.get("ai_assist", "none"),
                 "upload_session_id": sid_session,
                 "legacy_id": rec["legacy_id"]})
            self.app.store.attach_finalized(conn, sid_session, sid)
        self.app.reload()
        self.app.tech.run_checks(sid, digest)
        if self.app.proj.submissions[sid].latest_check.overall == "pass":
            self.app.detection.on_new_version(sid, digest)
        self.app.reload()
        self.submission_by_legacy[rec["legacy_id"]] = sid
        return self._done(submission_id=sid, sha256=digest)

    @staticmethod
    def _carry_sidecar(src: Path, target: Path) -> None:
        """视频侧车元数据随原件一起进入内容寻址存储，供 1080P 复核。"""
        sidecar = Path(str(src) + ".media.json")
        if sidecar.is_file():
            dst = Path(str(target) + ".media.json")
            if not dst.exists():
                dst.write_bytes(sidecar.read_bytes())

    @staticmethod
    def _slice(f: Path, i: int, n: int) -> bytes:
        data = f.read_bytes()
        size = (len(data) + n - 1) // n
        return data[i * size:(i + 1) * size]

    # ---- 同图改裁剪：新版本，不新作品 ------------------------------------

    def _ingest_revision(self, rec: dict[str, Any]) -> dict[str, Any]:
        sid = self.submission_by_legacy[rec["of_legacy"]]
        s = self.app.proj.submissions[sid]
        f = self.files[rec["file"]]
        media_type = "image" if f.suffix in (".png", ".jpg", ".bmp") else "video"
        sess = self.app.upload.init_upload(
            uploader=s.contact, media_type=media_type, filename=f.name,
            total_chunks=1, chunk_size=f.stat().st_size)
        self.app.upload.put_chunk(sess["session_id"], 0, f.read_bytes())
        row = self.app.store.get_session(sess["session_id"])
        assembled = self.app.upload._assemble(sess["session_id"], row)
        digest, size = _hash_file(assembled)
        if digest in s.version_hashes():
            assembled.unlink(missing_ok=True)
            return self._done(submission_id=sid, unchanged=True)
        target = self.app.blobs.object_path(digest)
        if not target.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            assembled.rename(target)
        else:
            assembled.unlink(missing_ok=True)
        with self.app.store.tx() as conn:
            self.app.store.append(
                conn, "submission", sid, FILE_REPLACED,
                f"legacy-import:{s.contact}",
                {"sha256": digest, "size": size, "filename": f.name,
                 "reason": rec.get("reason", "legacy-revision"),
                 "legacy_id": rec["legacy_id"]})
            self.app.store.attach_finalized(
                conn, sess["session_id"], f"replaces:{sid}")
        self.app.reload()
        self.app.tech.run_checks(sid, digest)
        if self.app.proj.submissions[sid].latest_check.overall == "pass":
            self.app.detection.on_new_version(sid, digest)
        self.app.reload()
        return self._done(submission_id=sid, sha256=digest, new_version=True)

    # ---- 多人主张原作 ----------------------------------------------------

    def _ingest_claim(self, rec: dict[str, Any]) -> dict[str, Any]:
        sid = self.submission_by_legacy[rec["submission_legacy"]]
        ids = []
        for c in rec["claims"]:
            ids.append(self.app.upload.raise_rights_claim(
                sid, c["claimant"], c.get("contact", ""), c["note"]))
        return self._done(submission_id=sid, claim_ids=ids,
                          open_claims=len(ids))

    # ---- 历史人工结论（定性只来自人） ------------------------------------

    def _ingest_manual_review(self, rec: dict[str, Any]) -> dict[str, Any]:
        sid = self.submission_by_legacy[rec["submission_legacy"]]
        s = self.app.proj.submissions[sid]
        if not s.open_review_ids:
            # 归档补录：没有未决任务时补开一个再裁决
            with self.app.store.tx() as conn:
                self.app.detection.open_review(
                    conn, sid, rec.get("reasons", ["manual-backlog"]),
                    {"legacy_id": rec["legacy_id"]},
                    actor=f"reviewer:{rec['reviewer']}")
            self.app.reload()
        # 归档人工结论对该件全部未决核验项逐项下结论
        closed = []
        for rid in list(self.app.proj.submissions[sid].open_review_ids):
            self.app.editorial.record_verdict(
                rid, rec["reviewer"], "reviewer",
                rec["verdict"], rec.get("note", "存量记录人工结论"))
            closed.append(rid)
        return self._done(submission_id=sid, verdict=rec["verdict"],
                          closed_reviews=closed)

    # ---- 历史选用/复核/投递 ----------------------------------------------

    def _ingest_selection(self, rec: dict[str, Any]) -> dict[str, Any]:
        sid = self.submission_by_legacy[rec["submission_legacy"]]
        s = self.app.proj.submissions[sid]
        digest = s.current.sha256
        lid = self.app.editorial.select(
            sid, digest, rec["channels"], rec["editor"], "editor",
            rec.get("note", "存量记录选用"))
        self.app.editorial.approve_selection(
            lid, rec["reviewer"], "reviewer", "存量记录复核通过")
        self.app.distribution.dispatch(lid, rec.get("publisher", "publisher"),
                                       "publisher")
        self.selection_by_legacy[rec["legacy_id"]] = lid
        return self._done(selection_id=lid, channels=rec["channels"])

    # ---- 渠道回执（部分失败；callback_id 幂等） --------------------------

    def _ingest_channel_results(self, rec: dict[str, Any]) -> dict[str, Any]:
        lid = self.selection_by_legacy[rec["selection_legacy"]]
        sel = self.app.proj.selections[lid]
        out = {}
        for ch_rec in rec["channels"]:
            did = sel.deliveries[ch_rec["channel"]]
            # 同 callback_id 重放会被幂等拦截
            res = self.app.distribution.record_result(
                did, ch_rec["result"], ch_rec["callback_id"],
                ch_rec.get("channel_ref"), ch_rec.get("detail"))
            out[ch_rec["channel"]] = res["state"]
        return self._done(selection_id=lid, channel_states=out)

    # ---- 历史版面进入 ----------------------------------------------------

    def _ingest_layout(self, rec: dict[str, Any]) -> dict[str, Any]:
        lid = self.selection_by_legacy[rec["selection_legacy"]]
        did = self.app.proj.selections[lid].deliveries[rec["channel"]]
        self.app.editorial.enter_layout(
            did, rec.get("publisher", "publisher"), "publisher",
            rec["edition"], rec["layout_ref"])
        return self._done(delivery_id=did, layout_ref=rec["layout_ref"])

    # ---- 历史撤稿：停发 + 渠道撤回，版面依据保留 --------------------------

    def _ingest_withdrawal(self, rec: dict[str, Any]) -> dict[str, Any]:
        sid = self.submission_by_legacy[rec["submission_legacy"]]
        s = self.app.proj.submissions[sid]
        if s.withdrawal is None:
            with self.app.store.tx() as conn:
                self.app.store.append(
                    conn, "submission", sid, WITHDRAWAL_REQUESTED,
                    f"legacy-import:{s.contact}",
                    {"reason": rec.get("reason", "存量撤稿申请"),
                     "requested_by": s.contact, "legacy_id": rec["legacy_id"]})
            self.app.reload()
        res = self.app.editorial.accept_withdrawal(
            sid, rec.get("reviewer", "reviewer"), "reviewer",
            rec.get("note", "存量撤稿受理"))
        # 已成功刊发的渠道登记撤回回执
        withdrawn = []
        for d in list(self.app.proj.deliveries.values()):
            if d.submission_id == sid and d.state in (
                    RESULT_SUCCEEDED, RESULT_FAILED):
                self.app.distribution.withdraw(
                    d.delivery_id, rec.get("publisher", "publisher"),
                    "publisher", "撤稿召回",
                    callback_id=rec.get("callback_id",
                                        f"legacy-wd-{rec['legacy_id']}-{d.channel}"))
                withdrawn.append(d.channel)
        return self._done(submission_id=sid, halted=True,
                          withdrawn_channels=withdrawn,
                          layout_kept=bool(s.layout_versions),
                          **{"halted_deliveries": res["halted_deliveries"]})


def load_manifest(path: str | Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))
