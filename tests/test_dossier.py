"""编辑卷宗：来源、授权版本、处理轨迹、传播去向四件事逐件可说明。"""

from tests.conftest import close_all_reviews, png_bytes, submit


def test_dossier_covers_provenance_authorization_trace_distribution(app):
    r = submit(app, png_bytes(seed=11), contact="d@example.org",
               display_name="卷宗作者", title="卷宗作品",
               note="手机拍摄")
    sid, token = r["submission_id"], r["contributor_token"]
    # 补说明
    app.upload.supplement_note(sid, token, "补：2026 年夏拍摄于河畔")
    # 人工核验通过
    close_all_reviews(app, sid)
    # 选用 -> 复核 -> 投递 -> 两个成功一个失败
    lid = app.editorial.select(
        sid, r["sha256"],
        ["campaign-page", "newspaper", "media-matrix"],
        "editor-sun", "editor", "重点作品")
    app.editorial.approve_selection(lid, "reviewer-qin", "reviewer", "通过")
    dids = app.distribution.dispatch(lid, "publisher-gao", "publisher")
    results = {"campaign-page": ("succeeded", "page/d"),
               "newspaper": ("succeeded", "RB-A9"),
               "media-matrix": ("failed", None)}
    for did in dids:
        ch = app.proj.deliveries[did].channel
        state, ref = results[ch]
        app.distribution.record_result(
            did, state, f"cb-{ch}-d", channel_ref=ref,
            detail=None if state == "succeeded" else "接口超时")
    # 版面
    paper_did = next(
        did for did in dids if app.proj.deliveries[did].channel == "newspaper")
    app.editorial.enter_layout(
        paper_did, "publisher-gao", "publisher", "2026-09-20 A版", "RB-A9")

    d = app.dossier(sid)

    # 来源
    prov = d["provenance"]
    assert prov["display_name"] == "卷宗作者"
    assert prov["upload_session_id"]
    assert d["files"]["current_sha256"] == r["sha256"]

    # 授权版本
    assert d["authorization"]["terms_version"] == "terms-2026-01"
    assert len(d["authorization"]["terms_digest"]) == 64
    assert d["authorization"]["non_commercial_only"] is True

    # 技术检查可复核
    assert d["tech_checks"][0]["checks"]

    # 轨迹：事件顺序包含关键节点，且时间线脱敏
    types = [e["event_type"] for e in d["timeline"]]
    assert types[:3] == ["SubmissionCreated", "TechCheckRecorded",
                         "ManualReviewRequired"]
    assert "NoteSupplemented" in types
    assert "VersionEnteredLayout" in types
    for e in d["timeline"]:
        nested = e["payload"].get("contributor", {})
        assert "contact" not in nested
        assert "contributor_token" not in e["payload"]

    # 传播去向与刊发依据
    states = {x["channel"]: x["state"] for x in d["distributions"]}
    assert states == {"campaign-page": "succeeded",
                      "newspaper": "succeeded",
                      "media-matrix": "failed"}
    assert d["layout"][0]["layout_ref"] == "RB-A9"
    assert d["layout"][0]["selected_by"] == "editor-sun"
    assert d["layout"][0]["approved_by"] == "reviewer-qin"
    assert d["selections"][0]["channel_states"]["media-matrix"] == "failed"
