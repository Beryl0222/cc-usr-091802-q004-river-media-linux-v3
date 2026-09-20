"""技术检查可复核性与内容哈希存储组织。"""

from pathlib import Path

from riverflow import media
from tests.conftest import png_bytes, submit, video_bytes


def test_image_tech_check_has_expected_and_actual(app):
    res = submit(app, png_bytes(width=2200, height=1500, seed=10))
    s = app.proj.submissions[res["submission_id"]]
    check = s.latest_check
    assert check.overall == "pass"
    by_id = {c["check_id"]: c for c in check.checks}
    assert by_id["image-resolution"]["expected"] == f">= {media.MIN_IMAGE_LONG_EDGE}px"
    assert by_id["image-resolution"]["actual"] == 2200
    # 主题项必须是人工态，不允许自动结论
    assert by_id["theme-fit"]["result"] == "manual"


def test_low_resolution_image_fails_with_evidence(app):
    res = submit(app, png_bytes(width=800, height=600, seed=11))
    s = app.proj.submissions[res["submission_id"]]
    assert s.status == "tech_failed"
    check = s.latest_check
    by_id = {c["check_id"]: c for c in check.checks}
    assert by_id["image-resolution"]["result"] == "fail"
    assert by_id["image-resolution"]["actual"] == 800


def test_video_1080p_check_with_sidecar_meta(app):
    data, meta = video_bytes(1920, 1080, 45)
    from tests.conftest import chunked_upload
    sid = chunked_upload(app, data, "video", "river.mp4",
                         "v@example.org", n=3, media_meta=meta)
    res = app.upload.finalize(
        sid, {"display_name": "影像作者", "contact": "v@example.org"},
        "terms-2026-01", title="长镜头")
    s = app.proj.submissions[res["submission_id"]]
    by_id = {c["check_id"]: c for c in s.latest_check.checks}
    assert by_id["video-resolution"]["actual"] == 1080
    assert by_id["video-resolution"]["result"] == "pass"
    assert by_id["video-duration"]["actual"] == 45
    # 侧车元数据随原件按内容哈希存放，分辨率证据可复核
    sidecar = app.blobs.object_path(res["sha256"])
    assert Path(str(sidecar) + ".media.json").is_file()


def test_720p_video_fails_resolution(app):
    data, meta = video_bytes(1280, 720, 30)
    from tests.conftest import chunked_upload
    sid = chunked_upload(app, data, "video", "low.mp4",
                         "v@example.org", media_meta=meta)
    res = app.upload.finalize(
        sid, {"display_name": "影像作者", "contact": "v@example.org"},
        "terms-2026-01")
    s = app.proj.submissions[res["submission_id"]]
    assert s.status == "tech_failed"
    by_id = {c["check_id"]: c for c in s.latest_check.checks}
    assert by_id["video-resolution"]["result"] == "fail"
    assert by_id["video-resolution"]["actual"] == 720


def test_originals_and_derivatives_organized_by_hash(app):
    res = submit(app, png_bytes(seed=12))
    digest = res["sha256"]
    obj = app.blobs.object_path(digest)
    assert obj.parent.name == digest[:2]
    assert obj.is_file()
    derivatives = app.blobs.derivatives(digest)
    assert any(d.startswith("thumb-320") for d in derivatives)
    # 缩略图内容不同于原件但归属于原件哈希目录
    assert (app.blobs.transcodes / digest).is_dir()
