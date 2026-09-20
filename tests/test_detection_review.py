"""相似/搬运/合成信号只能进人工核验，不能由分数定性。"""

import pytest

from riverflow.domain import SIGNAL_SIMILAR, SIGNAL_SYNTHETIC
from riverflow.eventstore import ConflictError
from tests.conftest import close_all_reviews, png_bytes, submit


def test_every_new_submission_gets_manual_originality_review(app):
    res = submit(app, png_bytes(seed=20))
    s = app.proj.submissions[res["submission_id"]]
    assert s.open_review_ids, "原创性与主题必须有人工核验任务"
    rid = s.open_review_ids[0]
    reasons = app.proj.reviews[rid].reasons
    assert "originality-confirmation" in reasons
    assert "theme-fit" in reasons


def test_similar_image_opens_review_with_score_as_evidence_only(app):
    r1 = submit(app, png_bytes(width=2200, height=1500, seed=22),
                contact="a@example.org", display_name="甲")
    close_all_reviews(app, r1["submission_id"])
    # seed 33 与 seed 22 在阈值内 -> 相似线索（kind 1 vs kind 2, dist 8）
    r2 = submit(app, png_bytes(width=2400, height=1600, seed=33),
                contact="b@example.org", display_name="乙")
    s2 = app.proj.submissions[r2["submission_id"]]
    sim = [sig for sig in s2.signals if sig["kind"] == SIGNAL_SIMILAR]
    assert sim, "相似图必须生成信号"
    sig = sim[0]
    assert sig["auto_conclusion"] is False
    assert "hamming_distance" in sig["evidence"]
    # 有信号且尚未人工裁决：状态必须是待人工核验
    assert s2.status == "manual_review"
    # 系统从未自动给出 copied/synthetic 结论
    assert s2.verdict is None


def test_identical_reupload_flagged_similar_but_waits_for_human(app):
    data = png_bytes(seed=44)
    r1 = submit(app, data, contact="first@example.org", display_name="首投")
    close_all_reviews(app, r1["submission_id"])
    r2 = submit(app, data, contact="second@example.org", display_name="二投")
    s2 = app.proj.submissions[r2["submission_id"]]
    assert s2.status == "manual_review"
    assert any(sig["kind"] == SIGNAL_SIMILAR for sig in s2.signals)
    # 分数再高也不能自动定性：编辑选用必须被拒绝
    with pytest.raises(ConflictError):
        app.editorial.select(
            r2["submission_id"], r2["sha256"], ["campaign-page"],
            "editor-sun", "editor")


def test_external_synthetic_score_never_auto_qualifies(app):
    r = submit(app, png_bytes(seed=44), contact="x@example.org")
    # 常规原创性核验先行关闭；外部模型随后上报合成线索，另开专项核验
    close_all_reviews(app, r["submission_id"])
    rid = app.detection.report_external(
        r["submission_id"], SIGNAL_SYNTHETIC, score=0.99,
        evidence={"model": "demo-detector", "note": "疑似生成纹理"},
        reporter="system:external-model")
    s = app.proj.submissions[r["submission_id"]]
    assert rid in s.open_review_ids
    # 0.99 高分也只是证据，结论仍为空
    assert s.verdict is None
    assert s.status == "manual_review"
    # 只有复核员的人工结论能定性
    app.editorial.record_verdict(rid, "reviewer-qin", "reviewer",
                                 "synthetic", "复核确认系 AI 合成")
    app.reload()
    s = app.proj.submissions[r["submission_id"]]
    assert s.verdict == "synthetic"
    assert s.status == "rejected"


def test_editor_cannot_record_verdict(app):
    r = submit(app, png_bytes(seed=22), contact="a@example.org")
    rid = app.proj.submissions[r["submission_id"]].open_review_ids[0]
    with pytest.raises(ConflictError):
        app.editorial.record_verdict(
            rid, "editor-sun", "editor", "authentic")


def test_reviewed_authentic_reaches_verified(app):
    r = submit(app, png_bytes(seed=11), contact="a@example.org")
    close_all_reviews(app, r["submission_id"])
    s = app.proj.submissions[r["submission_id"]]
    assert s.verdict == "authentic"
    assert s.status == "verified"
