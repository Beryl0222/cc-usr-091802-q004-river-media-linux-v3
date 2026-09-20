"""存量记录导入：四类历史记录处理后拿到可发布队列，且导入幂等。"""

from pathlib import Path

from riverflow.ingest import IngestService, load_manifest


def _run(app, tmp_path, manifest_path="fixtures/legacy_manifest.json"):
    manifest = load_manifest(manifest_path)
    ing = IngestService(app)
    ing.materialize(manifest, tmp_path / "legacy-files")
    return ing.ingest(manifest), ing


def test_ingest_legacy_manifest_and_queue(app, tmp_path):
    report, ing = _run(app, tmp_path)
    by_id = {r["legacy_id"]: r for r in report["imported"]}

    # 大文件续传：一件作品
    big = by_id["mail-1001"]
    assert big["submission_id"]

    # 同图改裁剪：同一作品产生新版本，而非第二件作品
    rev = by_id["mail-1003"]
    assert rev["new_version"] is True
    assert rev["submission_id"] == by_id["mail-1002"]["submission_id"]
    sid_photo = rev["submission_id"]
    s = app.proj.submissions[sid_photo]
    assert len(s.versions) == 2

    # 多人主张：两条独立事件，作品卡在人工核验
    claim = by_id["mail-1005"]
    assert claim["open_claims"] == 2
    disputed = app.proj.submissions[claim["submission_id"]]
    assert disputed.status == "manual_review"

    # 渠道部分失败：只有失败渠道进入可发布队列
    queue = report["publishable_queue"]
    assert len(queue) == 1
    q = queue[0]
    assert q["submission_id"] == big["submission_id"]
    assert q["channels_pending"] == ["media-matrix"]
    assert q["channel_states"]["newspaper"] == "succeeded"

    # 版面依据保留
    dossier = app.dossier(big["submission_id"])
    assert dossier["layout"][0]["layout_ref"] == "RB20260918-A3"
    assert dossier["layout"][0]["terms_version"] == app.config.terms_version

    # 撤稿作品停止分发且不在队列
    withdrawn_id = by_id["mail-1006"]["submission_id"]
    assert app.proj.submissions[withdrawn_id].halted is True


def test_ingest_is_idempotent_on_rerun(app, tmp_path):
    first, ing = _run(app, tmp_path)
    second = ing.ingest(load_manifest("fixtures/legacy_manifest.json"))
    assert all(r.get("skipped") == "already-ingested"
               for r in second["imported"])
    # 作品数不增加
    assert len(app.proj.submissions) == len({
        r.get("submission_id") for r in first["imported"]
        if r.get("submission_id") and r["type"] in ("legacy_upload",)})
    # 队列结论一致
    assert [q["selection_id"] for q in second["publishable_queue"]] == \
           [q["selection_id"] for q in first["publishable_queue"]]


def test_retry_failed_channel_clears_queue(app, tmp_path):
    _, ing = _run(app, tmp_path)
    # 找到失败的 matrix 投递，补一条成功回执
    manifest = load_manifest("fixtures/legacy_manifest.json")
    sel_legacy = "mail-1010"
    lid = ing.selection_by_legacy[sel_legacy]
    did = app.proj.selections[lid].deliveries["media-matrix"]
    out = app.distribution.record_result(
        did, "succeeded", "matrix-cb-retry-1", "mm/river-1001")
    assert out["deduped"] is False
    assert app.publishable_queue() == []
