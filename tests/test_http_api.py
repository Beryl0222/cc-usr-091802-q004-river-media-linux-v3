"""HTTP API 端到端：鉴权、职责分离、幂等键、公开接口脱敏。"""

import hashlib

from tests.conftest import (
    TERMS_VERSION, close_all_reviews, png_bytes, staff_headers, video_bytes,
)


def _upload_via_api(http, data, media_type="image", filename="p.png",
                    media_meta=None, n=4):
    size = (len(data) + n - 1) // n
    status, body = http.request("POST", "/v1/uploads", {
        "uploader": "api@example.org", "media_type": media_type,
        "filename": filename, "total_chunks": n, "chunk_size": size,
        "declared_sha256": hashlib.sha256(data).hexdigest(),
        "media_meta": media_meta})
    assert status == 201, body
    sid = body["session_id"]
    for i in range(n):
        st, b = http.request(
            "PUT", f"/v1/uploads/{sid}/chunks/{i}",
            raw=data[i * size:(i + 1) * size])
        assert st == 200, b
    return sid


def test_health_and_terms(http):
    st, body = http.request("GET", "/health")
    assert st == 200 and body["service"] == "river-media-flow"
    st, body = http.request("GET", "/terms")
    assert body["terms_version"] == TERMS_VERSION
    assert body["non_commercial_only"] is True


def test_full_upload_to_publish_pipeline(http):
    data = png_bytes(seed=11)
    sid = _upload_via_api(http, data)
    st, fin = http.request("POST", f"/v1/uploads/{sid}/finalize", {
        "contributor": {"display_name": "接口作者", "contact": "api@example.org"},
        "terms_version": TERMS_VERSION, "title": "接口作品"})
    assert st == 201 and fin["reused"] is False
    sub_id, token = fin["submission_id"], fin["contributor_token"]

    # 员工鉴权
    st, _ = http.request("GET", "/v1/staff/inbox")
    assert st == 401

    # 复核员作出人工结论
    st, reviews = http.request("GET", "/v1/staff/reviews/open",
                               headers=staff_headers("reviewer-qin", "reviewer"))
    assert st == 200
    rid = next(r["review_id"] for r in reviews["items"]
               if r["submission_id"] == sub_id)
    st, _ = http.request(
        "POST", f"/v1/staff/reviews/{rid}/verdict",
        {"verdict": "authentic", "note": "API 复核通过"},
        headers=staff_headers("reviewer-qin", "reviewer"))
    assert st == 201

    # 编辑选用（复核员不能选）
    st, err = http.request("POST", "/v1/staff/selections",
                           {"submission_id": sub_id, "sha256": fin["sha256"],
                            "channels": ["newspaper"]},
                           headers=staff_headers("reviewer-qin", "reviewer"))
    assert st == 409
    st, sel = http.request("POST", "/v1/staff/selections",
                           {"submission_id": sub_id, "sha256": fin["sha256"],
                            "channels": ["newspaper"]},
                           headers=staff_headers("editor-sun", "editor"))
    assert st == 201
    lid = sel["selection_id"]

    # 编辑不能自复核
    st, _ = http.request("POST", f"/v1/staff/selections/{lid}/approve", {},
                         headers=staff_headers("editor-sun", "editor"))
    assert st == 409
    st, _ = http.request("POST", f"/v1/staff/selections/{lid}/approve", {},
                         headers=staff_headers("reviewer-qin", "reviewer"))
    assert st == 201

    # 发布
    st, disp = http.request("POST", f"/v1/staff/selections/{lid}/dispatch",
                            headers=staff_headers("publisher-gao", "publisher"))
    assert st == 201
    did = disp["delivery_ids"][0]
    st, res = http.request(
        "POST", f"/v1/staff/deliveries/{did}/result",
        {"result": "succeeded", "callback_id": "api-cb-1",
         "channel_ref": "RB-API-1"},
        headers=staff_headers("publisher-gao", "publisher"))
    assert res["state"] == "succeeded"

    # 公开目录可见且无联系方式；投稿人公开编号可查
    st, cat = http.request("GET", "/public/catalog")
    item = next(i for i in cat["items"] if i["submission_id"] == sub_id)
    assert "contact" not in item
    st, status_view = http.request(
        "GET", f"/public/status?code={fin['public_code']}")
    assert status_view["adopted"] is True
    assert "contact" not in status_view

    # 卷宗含轨迹
    st, dossier = http.request(
        "GET", f"/v1/staff/submissions/{sub_id}/dossier",
        headers=staff_headers("editor-sun", "editor"))
    assert st == 200
    assert dossier["authorization"]["terms_digest"]


def test_idempotency_key_dedupes_selection(http):
    data = png_bytes(seed=22)
    sid = _upload_via_api(http, data)
    st, fin = http.request("POST", f"/v1/uploads/{sid}/finalize", {
        "contributor": {"display_name": "甲", "contact": "api2@example.org"},
        "terms_version": TERMS_VERSION})
    sub_id = fin["submission_id"]
    st, reviews = http.request("GET", "/v1/staff/reviews/open",
                               headers=staff_headers("r", "reviewer"))
    for r in reviews["items"]:
        if r["submission_id"] == sub_id:
            http.request("POST", f"/v1/staff/reviews/{r['review_id']}/verdict",
                         {"verdict": "authentic"},
                         headers=staff_headers("r", "reviewer"))
    payload = {"submission_id": sub_id, "sha256": fin["sha256"],
               "channels": ["newspaper"]}
    st, b1 = http.request("POST", "/v1/staff/selections", payload,
                          headers=staff_headers("editor-sun", "editor",
                                                idem="sel-1"))
    st, b2 = http.request("POST", "/v1/staff/selections", payload,
                          headers=staff_headers("editor-sun", "editor",
                                                idem="sel-1"))
    assert b1["selection_id"] == b2["selection_id"]


def test_contributor_endpoints_require_token(http):
    data = png_bytes(seed=33)
    sid = _upload_via_api(http, data)
    st, fin = http.request("POST", f"/v1/uploads/{sid}/finalize", {
        "contributor": {"display_name": "丙", "contact": "api3@example.org"},
        "terms_version": TERMS_VERSION})
    st, err = http.request(
        "POST", f"/v1/submissions/{fin['submission_id']}/notes",
        {"note": "无凭证补充"})
    assert st == 401


def test_external_detection_forces_manual_review(http):
    data = png_bytes(seed=44)
    sid = _upload_via_api(http, data)
    st, fin = http.request("POST", f"/v1/uploads/{sid}/finalize", {
        "contributor": {"display_name": "丁", "contact": "api4@example.org"},
        "terms_version": TERMS_VERSION})
    st, body = http.request("POST", "/v1/staff/detections", {
        "submission_id": fin["submission_id"], "kind": "reuse",
        "score": 0.87,
        "evidence": {"source": "图片搜索命中", "url": "https://example/x"}},
        headers=staff_headers("editor-sun", "editor"))
    assert st == 201 and body["auto_qualification"] is False
    st, inbox = http.request("GET", "/v1/staff/inbox",
                             headers=staff_headers("r", "reviewer"))
    row = next(i for i in inbox["items"]
               if i["submission_id"] == fin["submission_id"])
    assert row["status"] == "manual_review"


def test_video_upload_with_meta_passes_1080p(http):
    data, meta = video_bytes(1920, 1080, 60)
    sid = _upload_via_api(http, data, "video", "river.mp4",
                          media_meta=meta, n=2)
    st, fin = http.request("POST", f"/v1/uploads/{sid}/finalize", {
        "contributor": {"display_name": "影像", "contact": "v@example.org"},
        "terms_version": TERMS_VERSION})
    assert st == 201
    # 视频元数据到位后不应是技术不合格，而是进入常规人工核验
    st, inbox = http.request("GET", "/v1/staff/inbox",
                             headers=staff_headers("r", "reviewer"))
    row = next(i for i in inbox["items"]
               if i["submission_id"] == fin["submission_id"])
    assert row["status"] == "manual_review"
