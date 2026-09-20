"""媒体探针与感知指纹的零依赖实现单测。"""

from pathlib import Path

from riverflow import demo, media


def test_png_dimensions_and_decode(tmp_path):
    p = tmp_path / "p.png"
    demo.write_png(p, 320, 240, lambda x, y: (x % 256, y % 256, 128))
    info = media.probe(p, "image")
    assert (info.width, info.height) == (320, 240)
    decoded = media.decode_rgb(p)
    assert decoded is not None
    w, h, rgb = decoded
    assert (w, h) == (320, 240) and len(rgb) == 320 * 240 * 3


def test_dhash_same_image_distance_zero(tmp_path):
    p = tmp_path / "p.png"
    demo.write_png(p, 800, 600,
                   demo.river_photo(800, 600, 7))
    fp = media.dhash_fingerprint(p)
    assert fp is not None
    assert media.hamming_distance(fp, fp) == 0


def test_dhash_crop_is_closer_than_unrelated(tmp_path):
    w, h = 2400, 1600
    src = demo.river_photo(w, h, 11)
    p1 = tmp_path / "orig.png"
    demo.write_png(p1, w, h, src)
    p2 = tmp_path / "crop.png"

    def crop_px(x, y):
        return src(x + 46, y + 31)

    def crop_row(y, _src=src):
        row = _src.row_bytes(y + 31)
        return row[46 * 3:46 * 3 + 2000 * 3]

    crop_px.row_bytes = crop_row
    demo.write_png(p2, 2000, 1500, crop_px)
    p3 = tmp_path / "other.png"
    demo.write_png(p3, 3000, 2000, demo.river_photo(3000, 2000, 22))
    f1, f2, f3 = (media.dhash_fingerprint(p) for p in (p1, p2, p3))
    d_crop = media.hamming_distance(f1, f2)
    d_other = media.hamming_distance(f1, f3)
    assert d_crop <= media.DHASH_REVIEW_DISTANCE
    assert d_other > media.DHASH_REVIEW_DISTANCE


def test_unknown_format_returns_unknown(tmp_path):
    p = tmp_path / "x.bin"
    p.write_bytes(b"definitely not media" * 20)
    info = media.probe(p, "image")
    assert info.kind == "unknown"
    assert media.dhash_fingerprint(p) is None


def test_video_sidecar_meta(tmp_path):
    p = demo.write_video_stub(tmp_path / "v.mp4", 1920, 1080, 12)
    info = media.probe(p, "video")
    assert info.container == "mp4"
    assert info.height == 1080 and info.duration_seconds == 12
