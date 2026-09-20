"""编辑选用/复核/发布职责分离、版面冻结与停发。"""

import pytest

from riverflow.eventstore import ConflictError
from tests.conftest import close_all_reviews, png_bytes, submit


def _verified_work(app, seed=11, contact="a@example.org"):
    r = submit(app, png_bytes(seed=seed), contact=contact)
    close_all_reviews(app, r["submission_id"])
    return r


def test_editor_selects_then_different_reviewer_approves(app):
    r = _verified_work(app)
    sid = r["submission_id"]
    lid = app.editorial.select(
        sid, r["sha256"], ["campaign-page", "newspaper"],
        "editor-sun", "editor", "头版")
    app.editorial.approve_selection(
        lid, "reviewer-qin", "reviewer", "复核通过")
    sel = app.proj.selections[lid]
    assert sel.approved and sel.editor != sel.reviewer


def test_select_requires_verified(app):
    r = submit(app, png_bytes(seed=11))
    with pytest.raises(ConflictError):
        app.editorial.select(
            r["submission_id"], r["sha256"], ["newspaper"],
            "editor-sun", "editor")


def test_reviewer_cannot_select_and_editor_cannot_approve(app):
    r = _verified_work(app)
    with pytest.raises(ConflictError):
        app.editorial.select(
            r["submission_id"], r["sha256"], ["newspaper"],
            "reviewer-qin", "reviewer")
    lid = app.editorial.select(
        r["submission_id"], r["sha256"], ["newspaper"],
        "editor-sun", "editor")
    with pytest.raises(ConflictError):
        app.editorial.approve_selection(lid, "editor-sun", "editor")


def test_same_person_cannot_self_approve(app):
    r = _verified_work(app)
    lid = app.editorial.select(
        r["submission_id"], r["sha256"], ["newspaper"],
        "staff-wang", "editor")
    with pytest.raises(ConflictError):
        app.editorial.approve_selection(lid, "staff-wang", "reviewer")


def test_only_configured_noncommercial_channels(app):
    r = _verified_work(app)
    with pytest.raises(ConflictError):
        app.editorial.select(
            r["submission_id"], r["sha256"], ["paid-ad-network"],
            "editor-sun", "editor")


def test_dispatch_requires_approval_and_publisher_role(app):
    r = _verified_work(app)
    lid = app.editorial.select(
        r["submission_id"], r["sha256"], ["newspaper"],
        "editor-sun", "editor")
    with pytest.raises(ConflictError):
        app.distribution.dispatch(lid, "publisher-gao", "publisher")
    app.editorial.approve_selection(lid, "reviewer-qin", "reviewer")
    with pytest.raises(ConflictError):
        app.distribution.dispatch(lid, "editor-sun", "editor")
    ids = app.distribution.dispatch(lid, "publisher-gao", "publisher")
    assert len(ids) == 1


def test_layout_records_publish_basis_and_withdrawal_keeps_it(app):
    r = _verified_work(app)
    sid, token = r["submission_id"], r["contributor_token"]
    lid = app.editorial.select(
        sid, r["sha256"], ["newspaper"], "editor-sun", "editor")
    app.editorial.approve_selection(lid, "reviewer-qin", "reviewer")
    did = app.distribution.dispatch(lid, "publisher-gao", "publisher")[0]
    app.distribution.record_result(
        did, "succeeded", "cb-1", channel_ref="RB20260918-A3")
    app.editorial.enter_layout(
        did, "publisher-gao", "publisher", "2026-09-18 A版", "RB20260918-A3")

    app.upload.request_withdrawal(sid, token, "撤稿")
    app.editorial.accept_withdrawal(sid, "reviewer-qin", "reviewer")
    app.distribution.withdraw(did, "publisher-gao", "publisher", "撤稿召回",
                              callback_id="wd-1")
    app.reload()
    s = app.proj.submissions[sid]
    # 刊发依据保留
    assert s.layout_versions and s.layout_versions[0]["layout_ref"] == "RB20260918-A3"
    assert s.layout_versions[0]["terms_version"] == "terms-2026-01"
    # 渠道撤回，后续分发停止
    assert app.proj.deliveries[did].state == "withdrawn"
    assert s.halted is True
    # 队列中不再出现该选用单
    assert all(q["submission_id"] != sid for q in app.publishable_queue())
    # 撤回后禁止重新投递
    with pytest.raises(ConflictError):
        app.distribution.dispatch(lid, "publisher-gao", "publisher")
