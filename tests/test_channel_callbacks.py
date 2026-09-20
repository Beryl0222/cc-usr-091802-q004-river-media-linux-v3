"""渠道回执幂等汇总与部分失败重试。"""

from tests.conftest import close_all_reviews, png_bytes, submit


def _approved_delivery(app, channels, seed=11):
    r = submit(app, png_bytes(seed=seed))
    close_all_reviews(app, r["submission_id"])
    sid = r["submission_id"]
    lid = app.editorial.select(
        sid, r["sha256"], channels, "editor-sun", "editor")
    app.editorial.approve_selection(lid, "reviewer-qin", "reviewer")
    ids = app.distribution.dispatch(lid, "publisher-gao", "publisher")
    return r, lid, ids


def test_duplicate_callback_id_is_idempotent(app):
    _, lid, ids = _approved_delivery(app, ["campaign-page"])
    did = ids[0]
    first = app.distribution.record_result(
        did, "succeeded", "cb-page-9", channel_ref="page/1")
    duplicate = app.distribution.record_result(
        did, "succeeded", "cb-page-9", channel_ref="page/1")
    assert first["deduped"] is False
    assert duplicate["deduped"] is True
    d = app.proj.deliveries[did]
    assert len(d.results) == 1


def test_partial_failure_other_channels_still_succeed_and_queue_retry(app):
    channels = ["campaign-page", "newspaper", "media-matrix"]
    r, lid, ids = _approved_delivery(app, channels)
    by_channel = {}
    sel = app.proj.selections[lid]
    for ch in channels:
        by_channel[ch] = sel.deliveries[ch]
    app.distribution.record_result(
        by_channel["campaign-page"], "succeeded", "cb-1", "page/x")
    app.distribution.record_result(
        by_channel["newspaper"], "succeeded", "cb-2", "RB-A1")
    app.distribution.record_result(
        by_channel["media-matrix"], "failed", "cb-3", detail="超时")

    queue = app.publishable_queue()
    assert len(queue) == 1
    assert queue[0]["channels_pending"] == ["media-matrix"]
    states = queue[0]["channel_states"]
    assert states["campaign-page"] == "succeeded"
    assert states["media-matrix"] == "failed"

    # 重试成功后队列清空
    app.distribution.record_result(
        by_channel["media-matrix"], "succeeded", "cb-4", "mm/x")
    assert app.publishable_queue() == []


def test_late_failure_callback_does_not_override_success(app):
    _, _, ids = _approved_delivery(app, ["newspaper"], seed=22)
    did = ids[0]
    app.distribution.record_result(did, "succeeded", "cb-ok", "RB-A2")
    # 乱序/迟到的失败回执不能把成功翻回失败
    app.distribution.record_result(did, "failed", "cb-late", detail="迟到回执")
    assert app.proj.deliveries[did].state == "succeeded"


def test_failure_can_be_promoted_to_success(app):
    _, _, ids = _approved_delivery(app, ["media-matrix"], seed=33)
    did = ids[0]
    app.distribution.record_result(did, "failed", "cb-f1", detail="500")
    assert app.proj.deliveries[did].state == "failed"
    app.distribution.record_result(did, "succeeded", "cb-s1", "mm/y")
    assert app.proj.deliveries[did].state == "succeeded"


def test_withdrawn_is_terminal_against_later_callbacks(app):
    _, _, ids = _approved_delivery(app, ["campaign-page"], seed=44)
    did = ids[0]
    app.distribution.record_result(did, "succeeded", "cb-ok", "page/z")
    app.distribution.withdraw(did, "publisher-gao", "publisher", "撤稿", "wd-9")
    app.distribution.record_result(did, "succeeded", "cb-after-wd", "page/z")
    assert app.proj.deliveries[did].state == "withdrawn"
