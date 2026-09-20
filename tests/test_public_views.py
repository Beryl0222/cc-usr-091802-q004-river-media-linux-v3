"""公开查询脱敏：不暴露联系方式，不暴露未采用素材。"""

from tests.conftest import close_all_reviews, png_bytes, submit


def _publish_one(app, seed=11):
    r = submit(app, png_bytes(seed=seed), contact="pub@example.org",
               display_name="公示作者", title="刊发作品")
    close_all_reviews(app, r["submission_id"])
    lid = app.editorial.select(
        r["submission_id"], r["sha256"], ["campaign-page", "media-matrix"],
        "editor-sun", "editor")
    app.editorial.approve_selection(lid, "reviewer-qin", "reviewer")
    dids = app.distribution.dispatch(lid, "publisher-gao", "publisher")
    for i, did in enumerate(dids):
        ch = app.proj.deliveries[did].channel
        app.distribution.record_result(did, "succeeded", f"cb-{seed}-{ch}",
                                       channel_ref=f"{ch}/work-{seed}")
    return r


def test_catalog_lists_only_published_and_hides_contact(app):
    r_pub = _publish_one(app)
    # 另一件未被采用的作品
    r_hidden = submit(app, png_bytes(seed=33), contact="secret@example.org",
                      display_name="未采用作者", title="待审作品")
    close_all_reviews(app, r_hidden["submission_id"])  # 已核验但未选用

    catalog = app.public_catalog()
    ids = {item["submission_id"] for item in catalog}
    assert r_pub["submission_id"] in ids
    assert r_hidden["submission_id"] not in ids
    item = next(i for i in catalog if i["submission_id"] == r_pub["submission_id"])
    assert "contact" not in item and "token" not in item
    assert {p["channel"] for p in item["published"]} == {
        "campaign-page", "media-matrix"}


def test_failed_only_work_not_in_catalog(app):
    r = submit(app, png_bytes(seed=44), contact="f@example.org")
    close_all_reviews(app, r["submission_id"])
    lid = app.editorial.select(
        r["submission_id"], r["sha256"], ["media-matrix"],
        "editor-sun", "editor")
    app.editorial.approve_selection(lid, "reviewer-qin", "reviewer")
    did = app.distribution.dispatch(lid, "publisher-gao", "publisher")[0]
    app.distribution.record_result(did, "failed", "cb-x", detail="失败")
    assert app.public_catalog() == []


def test_contributor_status_query_by_code_is_redacted(app):
    r = _publish_one(app)
    s = app.proj.submissions[r["submission_id"]]
    view = app.public_status(s.public_code)
    assert view["adopted"] is True
    assert "contact" not in view and "contributor_token" not in view
    assert {p["channel"] for p in view["published"]} == {
        "campaign-page", "media-matrix"}


def test_unknown_public_code_returns_none(app):
    assert app.public_status("nope-nope") is None


def test_withdrawn_published_work_disappears_from_catalog(app):
    r = _publish_one(app, seed=11)
    sid, token = r["submission_id"], r["contributor_token"]
    # 撤稿并全部渠道撤回
    app.upload.request_withdrawal(sid, token, "撤稿")
    app.editorial.accept_withdrawal(sid, "reviewer-qin", "reviewer")
    for d in list(app.proj.deliveries.values()):
        if d.submission_id == sid:
            app.distribution.withdraw(
                d.delivery_id, "publisher-gao", "publisher", "召回",
                callback_id=f"wd-{d.channel}")
    app.reload()
    ids = {i["submission_id"] for i in app.public_catalog()}
    assert sid not in ids
