"""黄河影像征集后端的场景测试：续传、检测、授权、职责分离、回执与溯源。"""

import hashlib
import tempfile
import unittest

from riverflow import BlobStore, RiverflowService
from riverflow.core import Conflict, Forbidden, load_policy

TERMS = "投稿人授权编辑部在活动页、报纸与新媒体矩阵使用本作品，保留署名权。"
TERMS_V2 = "V2：补充允许二创剪辑的授权条款。"

EDITOR = {"id": "ed-1", "role": "editor"}
EDITOR2 = {"id": "ed-2", "role": "editor"}
REVIEWER = {"id": "rv-1", "role": "reviewer"}
PUBLISHER = {"id": "pb-1", "role": "publisher"}

VIDEO_PROBE = {"width": 1920, "height": 1080, "duration_sec": 30, "codec": "h264"}
IMAGE_PROBE = {"width": 4000, "height": 3000, "format": "jpeg"}


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class BaseCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.svc = RiverflowService(BlobStore(self.tmp.name), load_policy("fixtures/sample.json"))

    def contributor(self, name="张三", contact=None):
        return self.svc.register_contributor(
            name, contact or {"email": f"{name}@example.com", "phone": "13800000000"}
        )["id"]

    def submit(self, contributor_id, payload: bytes, *, title="作品", media_type="video",
               probe=None, terms=TERMS, version="v2026-09", scope=None):
        """走完整续传流程：开会话、分两片上传、完成。"""
        session = self.svc.open_upload(contributor_id, sha(payload), len(payload))
        if session["status"] != "complete":  # 已完成的会话直接幂等返回
            mid = len(payload) // 2
            self.svc.upload_chunk(session["id"], 0, payload[:mid])
            self.svc.upload_chunk(session["id"], mid, payload[mid:])
        return self.svc.complete_upload(
            session["id"], title=title, description="", media_type=media_type,
            probe=probe or VIDEO_PROBE, terms_version=version, terms_text=terms,
            license_scope=scope,
        )

    def publish_ready(self, contributor_id, payload=b"video-bytes-1080p", **kw):
        work = self.submit(contributor_id, payload, **kw)
        self.svc.select(work["id"], EDITOR)
        self.svc.approve(work["id"], REVIEWER)
        return work


class UploadDedupTest(BaseCase):
    def test_resumable_upload_never_duplicates_work(self):
        cid = self.contributor()
        payload = b"river" * 4096
        digest = sha(payload)

        first = self.svc.open_upload(cid, digest, len(payload))
        self.svc.upload_chunk(first["id"], 0, payload[:100])
        # 断线后重开：同一（投稿人, 哈希）返回同一会话，已传字节保留
        resumed = self.svc.open_upload(cid, digest, len(payload))
        self.assertEqual(resumed["id"], first["id"])
        self.assertEqual(resumed["received_bytes"], 100)

        self.svc.upload_chunk(first["id"], 100, payload[100:])
        work = self.svc.complete_upload(
            first["id"], title="壶口瀑布", description="", media_type="video",
            probe=VIDEO_PROBE, terms_version="v2026-09", terms_text=TERMS,
        )
        again = self.svc.complete_upload(
            first["id"], title="重复完成", description="", media_type="video",
            probe=VIDEO_PROBE, terms_version="v2026-09", terms_text=TERMS,
        )
        self.assertEqual(work["id"], again["id"])
        self.assertEqual(len(self.svc.works), 1)
        self.assertEqual(len(self.svc.assets), 1)  # 原件按哈希只存一份

    def test_hash_mismatch_blocks_completion(self):
        cid = self.contributor()
        payload = b"authentic-bytes"
        session = self.svc.open_upload(cid, sha(b"other"), len(payload))
        self.svc.upload_chunk(session["id"], 0, payload)
        with self.assertRaises(Conflict):
            self.svc.complete_upload(
                session["id"], title="t", description="", media_type="video",
                probe=VIDEO_PROBE, terms_version="v1", terms_text=TERMS,
            )

    def test_content_addressed_storage_layout(self):
        cid = self.contributor()
        payload = b"image-bytes"
        self.submit(cid, payload, media_type="image", probe=IMAGE_PROBE)
        digest = sha(payload)
        self.assertTrue(self.svc.blobs.exists(digest))
        derived = self.svc.blobs.put_derivative(digest, "transcode/1080p-h264", b"derived")
        path = self.svc.blobs.root / "derivatives" / digest / "transcode/1080p-h264" / derived
        self.assertTrue(path.exists())


class TechCheckTest(BaseCase):
    def test_video_below_1080p_fails_with_reviewable_report(self):
        cid = self.contributor()
        work = self.submit(cid, b"low-res-video",
                           probe={"width": 1280, "height": 720, "duration_sec": 10, "codec": "h264"})
        self.assertFalse(work["checks_passed"])
        report = self.svc.provenance(work["id"])["versions"][0]["check_report"]
        height_check = next(r for r in report["results"] if r["name"] == "min_height")
        self.assertEqual(height_check["measured"], 720)
        self.assertEqual(height_check["expected"], ">= 1080")
        self.assertEqual(report["ruleset_version"], "tech-rules/2026-09-01")

    def test_1080p_video_passes_and_is_selectable(self):
        cid = self.contributor()
        work = self.submit(cid, b"full-hd-video")
        self.assertTrue(work["checks_passed"])
        self.svc.select(work["id"], EDITOR)
        self.assertEqual(self.svc.get_work(work["id"])["status"], "selected")


class ManualReviewOnlyTest(BaseCase):
    def test_recropped_same_image_goes_to_manual_review_not_verdict(self):
        author = self.contributor("原作者")
        copier = self.contributor("搬运者")
        original = self.submit(author, b"original-image", media_type="image",
                               probe={**IMAGE_PROBE, "phash": "ffffffffffffffff"})
        recrop = self.submit(copier, b"recropped-image", media_type="image",
                             probe={**IMAGE_PROBE, "phash": "fffffffffffffffe"})
        # 同图改裁剪：只产生人工核验任务，作品不被自动定性
        self.assertEqual(recrop["status"], "received")
        self.assertEqual(recrop["open_review_tasks"], 1)
        task = next(iter(self.svc.review_tasks.values()))
        self.assertEqual(task["kind"], "suspected_repost")
        self.assertEqual(task["evidence"]["other_work_id"], original["id"])
        with self.assertRaises(Conflict):
            self.svc.select(recrop["id"], EDITOR)
        # 人工澄清后才可进入编辑流程
        self.svc.resolve_review_task(task["id"], REVIEWER, "cleared", note="确为本人再创作")
        self.svc.select(recrop["id"], EDITOR)

    def test_suspected_synthetic_requires_human_decision(self):
        cid = self.contributor()
        work = self.submit(cid, b"ai-image", media_type="image",
                           probe={**IMAGE_PROBE, "generator": "Midjourney v6"})
        self.assertEqual(work["status"], "received")  # 分数不直接定性
        task = next(iter(self.svc.review_tasks.values()))
        self.assertEqual(task["kind"], "suspected_synthetic")
        self.svc.resolve_review_task(task["id"], REVIEWER, "confirmed_violation")
        self.assertEqual(self.svc.get_work(work["id"])["status"], "rejected")  # 人工定性

    def test_exact_reupload_by_same_author_returns_same_work(self):
        cid = self.contributor()
        payload = b"same-bytes"
        first = self.submit(cid, payload, title="第一次")
        second = self.submit(cid, payload, title="重复上传")
        self.assertEqual(first["id"], second["id"])  # 幂等：不重复生成作品
        self.assertEqual(len(self.svc.works), 1)
        self.assertEqual(len(self.svc.assets), 1)  # 原件只存一份

    def test_exact_copy_by_another_contributor_flags_repost(self):
        author = self.contributor("原作者")
        copier = self.contributor("搬运者")
        payload = b"copied-bytes"
        original = self.submit(author, payload, title="原作")
        copy = self.submit(copier, payload, title="搬运")
        self.assertNotEqual(original["id"], copy["id"])
        self.assertEqual(len(self.svc.assets), 1)  # 内容寻址：同一份原件
        task = next(iter(self.svc.review_tasks.values()))
        self.assertEqual(task["kind"], "suspected_repost")
        self.assertEqual(task["evidence"]["match"], "exact_hash")
        self.assertEqual(copy["status"], "received")  # 仍待人工定性


class LicenseAndEventsTest(BaseCase):
    def test_license_bound_to_submission_terms(self):
        cid = self.contributor()
        work = self.submit(cid, b"licensed-video", version="v2026-09")
        provenance = self.svc.provenance(work["id"])
        grant = provenance["licenses"][0]
        self.assertEqual(grant["terms_version"], "v2026-09")
        self.assertEqual(grant["terms_sha256"], sha(TERMS.encode()))
        self.assertEqual(grant["scope"], ["campaign-page", "newspaper", "media-matrix"])

    def test_notes_replacement_dispute_takedown_each_form_events(self):
        cid = self.contributor()
        work = self.publish_ready(cid)
        wid = work["id"]
        self.svc.add_note(wid, {"id": cid, "role": "contributor"}, "补传：拍摄于汛期")
        self.svc.replace_file(wid, {"id": cid, "role": "contributor"},
                              b"replacement-video", note="替换为无水印版")
        self.svc.open_dispute(wid, REVIEWER, "李四", "主张该画面为其拍摄")
        self.svc.resolve_dispute(wid, REVIEWER, "confirmed_original")
        self.svc.request_takedown(wid, {"id": cid, "role": "contributor"}, "个人原因撤回")
        self.svc.execute_takedown(wid, REVIEWER)
        types = [e["type"] for e in self.svc.provenance(wid)["events"]]
        for expected in ("note_added", "file_replaced", "ownership_dispute_opened",
                         "ownership_dispute_resolved", "takedown_requested", "takedown_executed"):
            self.assertIn(expected, types)

    def test_replace_file_resets_progress_but_keeps_published_basis(self):
        cid = self.contributor()
        work = self.publish_ready(cid, scope=["campaign-page"])
        pubs = self.svc.publish(work["id"], PUBLISHER)
        self.svc.record_receipt(channel="campaign-page",
                                publication_key=pubs[0]["idempotency_key"],
                                receipt_id="rc-1", status="success")
        self.svc.replace_file(work["id"], {"id": cid, "role": "contributor"},
                              b"v2-video-bytes", note="修订版")
        view = self.svc.get_work(work["id"])
        self.assertEqual(view["status"], "received")  # 需重新选用复核
        self.assertEqual(view["current_version"], 2)
        distribution = self.svc.provenance(work["id"])["distribution"]
        self.assertEqual(distribution[0]["version_no"], 1)  # 已入版面版本依据保留
        self.assertEqual(distribution[0]["basis"]["terms_version"], "v2026-09")


class TakedownTest(BaseCase):
    def test_takedown_keeps_basis_and_stops_distribution(self):
        cid = self.contributor()
        work = self.publish_ready(cid)
        wid = work["id"]
        pubs = self.svc.publish(wid, PUBLISHER)
        keys = {p["channel"]: p["idempotency_key"] for p in pubs}
        self.svc.record_receipt(channel="campaign-page",
                                publication_key=keys["campaign-page"],
                                receipt_id="rc-ok", status="success")
        self.svc.request_takedown(wid, {"id": cid, "role": "contributor"}, "权属待确认")
        self.svc.execute_takedown(wid, REVIEWER)

        with self.assertRaises(Conflict):
            self.svc.publish(wid, PUBLISHER)
        self.assertEqual(self.svc.publishable_queue(), [])
        self.assertEqual(self.svc.public_works(), [])  # 公开页不再展示

        distribution = {p["channel"]: p for p in self.svc.provenance(wid)["distribution"]}
        self.assertEqual(distribution["campaign-page"]["status"], "succeeded")  # 已刊发保留
        self.assertEqual(distribution["campaign-page"]["basis"]["terms_version"], "v2026-09")
        self.assertEqual(distribution["newspaper"]["status"], "cancelled")  # 未发渠道停止
        self.assertEqual(distribution["media-matrix"]["status"], "cancelled")


class RoleSeparationTest(BaseCase):
    def test_select_approve_publish_are_separate_duties(self):
        cid = self.contributor()
        work = self.submit(cid, b"role-video")
        wid = work["id"]
        with self.assertRaises(Forbidden):
            self.svc.select(wid, REVIEWER)
        with self.assertRaises(Forbidden):
            self.svc.approve(wid, EDITOR)
        self.svc.select(wid, EDITOR)
        with self.assertRaises(Forbidden):  # 同一人不能既选用又复核
            self.svc.approve(wid, {"id": "ed-1", "role": "reviewer"})
        self.svc.approve(wid, REVIEWER)
        with self.assertRaises(Forbidden):
            self.svc.publish(wid, EDITOR)
        pubs = self.svc.publish(wid, PUBLISHER)
        self.assertEqual(len(pubs), 3)

    def test_publish_never_exceeds_license_scope(self):
        cid = self.contributor()
        work = self.publish_ready(cid, scope=["campaign-page"])
        with self.assertRaises(Forbidden):
            self.svc.publish(work["id"], PUBLISHER, channels=["newspaper"])


class ReceiptTest(BaseCase):
    def test_partial_failure_and_idempotent_aggregation(self):
        cid = self.contributor()
        work = self.publish_ready(cid)
        wid = work["id"]
        pubs = self.svc.publish(wid, PUBLISHER)
        keys = {p["channel"]: p["idempotency_key"] for p in pubs}

        ok = self.svc.record_receipt(channel="campaign-page",
                                     publication_key=keys["campaign-page"],
                                     receipt_id="rc-1", status="success")
        self.assertEqual(ok["status"], "succeeded")
        dup = self.svc.record_receipt(channel="campaign-page",
                                      publication_key=keys["campaign-page"],
                                      receipt_id="rc-1", status="success")
        self.assertTrue(dup["deduplicated"])
        self.assertEqual(dup["receipts_count"], 1)  # 重复回执不重复计数

        failed = self.svc.record_receipt(channel="newspaper",
                                         publication_key=keys["newspaper"],
                                         receipt_id="rc-2", status="failure",
                                         detail="版面超时")
        self.assertEqual(failed["status"], "failed")

        queue = self.svc.publishable_queue()
        self.assertEqual(queue[0]["channels"]["media-matrix"], "sent")  # 部分失败可见

        # 失败渠道重发后补回成功回执
        self.svc.publish(wid, PUBLISHER, channels=["newspaper"])
        retried = self.svc.record_receipt(channel="newspaper",
                                          publication_key=keys["newspaper"],
                                          receipt_id="rc-3", status="success")
        self.assertEqual(retried["status"], "succeeded")
        self.assertEqual(retried["receipts_count"], 2)


class DisputeTest(BaseCase):
    def test_multiple_claimants_lock_work_until_resolved(self):
        cid = self.contributor("张三")
        rival = self.contributor("李四")
        work = self.publish_ready(cid)
        wid = work["id"]
        first = self.svc.open_dispute(wid, REVIEWER, "李四", "主张原作")
        second = self.svc.open_dispute(wid, REVIEWER, "王五", "亦主张原作")
        self.assertEqual(first["id"], second["id"])  # 多人主张归入同一案件
        self.assertEqual(len(second["claims"]), 2)
        with self.assertRaises(Conflict):
            self.svc.publish(wid, PUBLISHER)
        self.svc.resolve_dispute(wid, REVIEWER, "reassigned",
                                 confirmed_contributor_id=rival)
        provenance = self.svc.provenance(wid)
        self.assertEqual(provenance["source"]["contributor_id"], rival)
        self.assertFalse(self.svc.get_work(wid)["locked"])


class PublicQueryTest(BaseCase):
    def test_public_query_hides_contact_and_unadopted(self):
        cid = self.contributor("张三", {"email": "zs@example.com", "phone": "13800000000"})
        adopted = self.publish_ready(cid, b"adopted-video", title="采用作品")
        self.submit(cid, b"unadopted-video", title="未采用作品")
        pubs = self.svc.publish(adopted["id"], PUBLISHER, channels=["campaign-page"])
        self.svc.record_receipt(channel="campaign-page",
                                publication_key=pubs[0]["idempotency_key"],
                                receipt_id="rc-1", status="success")
        works = self.svc.public_works()
        self.assertEqual(len(works), 1)
        self.assertEqual(works[0]["title"], "采用作品")
        self.assertEqual(works[0]["author"], "张三")
        self.assertNotIn("contact", works[0])
        self.assertNotIn("email", str(works[0]))


class QueueAndProvenanceTest(BaseCase):
    def test_publishable_queue_and_full_provenance(self):
        cid = self.contributor()
        work = self.publish_ready(cid)
        wid = work["id"]
        queue = self.svc.publishable_queue()
        self.assertEqual([q["work_id"] for q in queue], [wid])
        self.assertEqual(queue[0]["licensed_channels"],
                         ["campaign-page", "newspaper", "media-matrix"])

        pubs = self.svc.publish(wid, PUBLISHER, channels=["campaign-page"])
        self.svc.record_receipt(channel="campaign-page",
                                publication_key=pubs[0]["idempotency_key"],
                                receipt_id="rc-1", status="success")

        provenance = self.svc.provenance(wid)
        self.assertEqual(provenance["source"]["upload_sessions"][0]["status"], "complete")
        self.assertEqual(provenance["licenses"][0]["terms_version"], "v2026-09")
        event_types = {e["type"] for e in provenance["events"]}
        self.assertTrue({"work_created", "selected", "approved",
                         "publication_created", "receipt_recorded"} <= event_types)
        self.assertEqual(provenance["distribution"][0]["status"], "succeeded")
        self.assertEqual(provenance["distribution"][0]["channel"], "campaign-page")


if __name__ == "__main__":
    unittest.main()
