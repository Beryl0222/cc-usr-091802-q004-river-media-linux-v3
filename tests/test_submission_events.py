"""投稿人侧事件：补说明、换文件、权属争议、撤稿。"""

import pytest

from riverflow.eventstore import ConflictError
from tests.conftest import close_all_reviews, png_bytes, submit


def test_supplement_note_appends_event(app):
    r = submit(app, png_bytes(seed=30), note="初版说明")
    sid, token = r["submission_id"], r["contributor_token"]
    app.upload.supplement_note(sid, token, "补充：同行两人共同拍摄")
    s = app.proj.submissions[sid]
    assert "初版说明" in s.note and "共同拍摄" in s.note
    types = [e["event_type"] for e in app.store.events("submission", sid)]
    assert "NoteSupplemented" in types


def test_note_requires_token(app):
    r = submit(app, png_bytes(seed=31))
    with pytest.raises(ConflictError):
        app.upload.supplement_note(r["submission_id"], "wrong-token", "x")


def test_replace_file_creates_new_version_not_new_work(app):
    r = submit(app, png_bytes(width=2000, height=1500, seed=11))
    sid, token = r["submission_id"], r["contributor_token"]
    close_all_reviews(app, sid)
    crop = png_bytes(width=1800, height=1400, seed=11, shift=(46, 31))
    from tests.conftest import chunked_upload
    repl_session = chunked_upload(
        app, crop, "image", "crop.png", "author@example.org")
    out = app.upload.replace_from_session(
        sid, token, repl_session, "报纸竖版重新构图")
    s = app.proj.submissions[sid]
    assert len(s.versions) == 2
    assert s.current.sha256 == out["sha256"]
    # 原件仍在
    assert app.blobs.has_object(r["sha256"])
    # 新版本重新进入人工核验
    assert s.open_review_ids
    review = app.proj.reviews[s.open_review_ids[0]]
    assert "similar" in review.reasons or "originality-confirmation" in review.reasons


def test_replace_with_identical_bytes_rejected(app):
    data = png_bytes(seed=11)
    r = submit(app, data)
    sid, token = r["submission_id"], r["contributor_token"]
    from tests.conftest import chunked_upload
    repl = chunked_upload(app, data, "image", "same.png",
                          "author@example.org")
    with pytest.raises(ConflictError):
        app.upload.replace_from_session(sid, token, repl, "重复替换")


def test_multiple_rights_claims_are_separate_events_and_block_selection(app):
    r = submit(app, png_bytes(seed=44))
    sid = r["submission_id"]
    close_all_reviews(app, sid)
    c1 = app.upload.raise_rights_claim(
        sid, "赵工", "zhao@example.org", "本人拍摄")
    c2 = app.upload.raise_rights_claim(
        sid, "某工作室", "studio@example.net", "职务作品主张")
    s = app.proj.submissions[sid]
    assert {c.claim_id for c in s.rights_claims} == {c1, c2}
    assert len(s.open_claims) == 2
    assert s.status == "manual_review"
    with pytest.raises(ConflictError):
        app.editorial.select(sid, s.current.sha256, ["newspaper"],
                             "editor-sun", "editor")


def test_claim_upheld_marks_work_not_original(app):
    r = submit(app, png_bytes(seed=44))
    sid = r["submission_id"]
    close_all_reviews(app, sid)
    cid = app.upload.raise_rights_claim(
        sid, "第三方", "c@example.org", "图片搬自我的图库")
    app.editorial.resolve_rights_claim(
        sid, cid, "reviewer-qin", "reviewer", "upheld", "证据成立")
    app.reload()
    s = app.proj.submissions[sid]
    assert s.verdict == "copied"
    assert s.status == "rejected"


def test_claim_rejected_reopens_path_to_verified(app):
    r = submit(app, png_bytes(seed=33))
    sid = r["submission_id"]
    close_all_reviews(app, sid)
    cid = app.upload.raise_rights_claim(
        sid, "异议人", "x@example.org", "怀疑搬运")
    app.editorial.resolve_rights_claim(
        sid, cid, "reviewer-qin", "reviewer", "rejected", "异议不成立")
    app.reload()
    assert app.proj.submissions[sid].status == "verified"


def test_withdrawal_request_then_accept_halts_distribution(app):
    r = submit(app, png_bytes(seed=11))
    sid, token = r["submission_id"], r["contributor_token"]
    close_all_reviews(app, sid)
    app.upload.request_withdrawal(sid, token, "个人原因")
    s = app.proj.submissions[sid]
    assert s.withdrawal["accepted"] is False
    # 撤稿申请存在即不可再选用
    with pytest.raises(ConflictError):
        app.editorial.select(sid, s.current.sha256, ["newspaper"],
                             "editor-sun", "editor")
    out = app.editorial.accept_withdrawal(
        sid, "reviewer-qin", "reviewer")
    app.reload()
    s = app.proj.submissions[sid]
    assert s.withdrawal["accepted"] is True
    assert s.halted is True


def test_double_withdrawal_request_rejected(app):
    r = submit(app, png_bytes(seed=11))
    sid, token = r["submission_id"], r["contributor_token"]
    app.upload.request_withdrawal(sid, token, "原因")
    with pytest.raises(ConflictError):
        app.upload.request_withdrawal(sid, token, "再次申请")
