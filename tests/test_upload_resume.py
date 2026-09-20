"""断点续传与"不重复生成作品"。"""

import hashlib

import pytest

from riverflow.eventstore import ConflictError
from tests.conftest import TERMS_VERSION, png_bytes, submit


def test_resume_with_interleaved_status(app):
    data = png_bytes()
    n = 5
    size = (len(data) + n - 1) // n
    sess = app.upload.init_upload(
        uploader="a@example.org", media_type="image", filename="p.png",
        total_chunks=n, chunk_size=size)
    sid = sess["session_id"]

    # 模拟断点：先传 0,1，客户端掉线后重查状态
    app.upload.put_chunk(sid, 0, data[0:size])
    app.upload.put_chunk(sid, 1, data[size:2 * size])
    status = app.upload.status(sid)
    assert status["missing_chunks"] == [2, 3, 4]

    # 续传时重发第 1 片：幂等接收、不报错
    again = app.upload.put_chunk(sid, 1, data[size:2 * size])
    assert again["stored"] is False
    for i in (2, 3, 4):
        app.upload.put_chunk(sid, i, data[i * size:(i + 1) * size])

    res = app.upload.finalize(
        sid, {"display_name": "甲", "contact": "a@example.org"},
        terms_version=TERMS_VERSION, title="作品")
    assert res["reused"] is False
    assert res["sha256"] == hashlib.sha256(data).hexdigest()


def test_finalize_is_idempotent_for_same_session(app):
    data = png_bytes(seed=2)
    from tests.conftest import chunked_upload
    sid = chunked_upload(app, data, "image", "p.png", "a@example.org")
    contributor = {"display_name": "甲", "contact": "a@example.org"}
    first = app.upload.finalize(sid, contributor, TERMS_VERSION, title="作品")
    second = app.upload.finalize(sid, contributor, TERMS_VERSION, title="作品")
    assert first["submission_id"] == second["submission_id"]
    assert second["reused"] is True
    assert len(app.proj.submissions) == 1


def test_identical_content_from_same_contributor_dedupes(app):
    data = png_bytes(seed=3)
    from tests.conftest import chunked_upload
    s1 = chunked_upload(app, data, "image", "a.png", "dup@example.org")
    r1 = app.upload.finalize(s1, {"display_name": "丁", "contact": "dup@example.org"},
                             TERMS_VERSION)
    s2 = chunked_upload(app, data, "image", "a-copy.png", "dup@example.org")
    r2 = app.upload.finalize(s2, {"display_name": "丁", "contact": "dup@example.org"},
                             TERMS_VERSION)
    assert r2["reused"] is True
    assert r1["submission_id"] == r2["submission_id"]
    assert len(app.proj.submissions) == 1
    # 内容只落一份原件
    assert app.blobs.has_object(r1["sha256"])


def test_declared_hash_mismatch_rejected(app):
    data = png_bytes(seed=4)
    from tests.conftest import chunked_upload
    sid = chunked_upload(
        app, data, "image", "p.png", "a@example.org",
        declared_sha256="0" * 64)
    with pytest.raises(ConflictError):
        app.upload.finalize(
            sid, {"display_name": "甲", "contact": "a@example.org"},
            TERMS_VERSION)
    # 被拒绝后没有产生作品
    assert app.proj.submissions == {}


def test_missing_chunks_blocks_finalize(app):
    data = png_bytes(seed=5)
    n = 3
    size = (len(data) + n - 1) // n
    sess = app.upload.init_upload(
        uploader="a@example.org", media_type="image", filename="p.png",
        total_chunks=n, chunk_size=size)
    app.upload.put_chunk(sess["session_id"], 0, data[:size])
    with pytest.raises(ConflictError):
        app.upload.finalize(
            sess["session_id"],
            {"display_name": "甲", "contact": "a@example.org"}, TERMS_VERSION)


def test_stale_terms_version_rejected(app):
    res = submit(app, png_bytes(seed=6))
    # 上面用当前版本成功；旧条款版本必须被拒（授权关联"提交时"有效条款）
    from tests.conftest import chunked_upload
    sid = chunked_upload(app, png_bytes(seed=7), "image", "p.png",
                         "old@example.org")
    with pytest.raises(ConflictError):
        app.upload.finalize(
            sid, {"display_name": "旧", "contact": "old@example.org"},
            terms_version="terms-2019-ancient")
