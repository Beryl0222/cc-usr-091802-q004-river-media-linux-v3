"""零依赖测试素材生成：合成 PNG（8 位 RGB）图像与视频侧车元数据。

仅用于本地演示与测试；生成的图片是程序化合成图，技术检查只读取像素指标，
"是否为 AI 合成"的结论仍只能由人工核验给出。
"""

from __future__ import annotations

import json
import struct
import zlib
from pathlib import Path


def write_png(path: str | Path, width: int, height: int,
              pixel_fn, level: int = 1) -> Path:
    """pixel_fn(x, y) -> (r, g, b)；若提供 ``pixel_fn.row_bytes(y)``
    则直接取整行 RGB 字节，避免逐像素 Python 循环。"""
    raw = bytearray()
    fast_rows = getattr(pixel_fn, "row_bytes", None)
    if fast_rows is not None:
        for y in range(height):
            raw.append(0)
            raw.extend(fast_rows(y))
    else:
        for y in range(height):
            raw.append(0)  # filter type 0
            for x in range(width):
                raw.extend(pixel_fn(x, y))
    compressed = zlib.compress(bytes(raw), level)

    def chunk(tag: bytes, data: bytes) -> bytes:
        c = struct.pack(">I", len(data)) + tag + data
        c += struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        return c

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    payload = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
               + chunk(b"IDAT", compressed) + chunk(b"IEND", b""))
    path = Path(path)
    path.write_bytes(payload)
    return path


def write_video_stub(path: str | Path, width: int, height: int,
                     duration_seconds: float, has_audio: bool = True,
                     container: str = "mp4") -> Path:
    """写出带 ftyp 的最小视频容器占位 + <file>.media.json 侧车。

    占位文件本身不是可播放视频；生产部署中真实视频由 ffprobe 适配，
    本项目的技术检查契约（期望 1080P、证据可复核）不变。
    """
    path = Path(path)
    brand = {"mp4": b"isom", "mov": b"qt  "}.get(container, b"isom")
    body = (b"\x00\x00\x00\x18ftyp" + brand + b"\x00\x00\x00\x00"
            + brand + b"\x00\x00\x00\x00")
    body += b"RIVER-MEDIA-DEMO-STUB" * 64
    path.write_bytes(body)
    Path(str(path) + ".media.json").write_text(json.dumps({
        "width": width, "height": height,
        "duration_seconds": duration_seconds,
        "has_audio": has_audio, "container": container,
        "codec": "h264",
    }, ensure_ascii=False), encoding="utf-8")
    return path


def river_photo(width: int, height: int, seed: int):
    """生成一张黄河主题风格的大尺度光影照片（四类构图由 seed 决定）。

    平滑的块状光影模拟真实光学照片：同一底图错位裁剪后感知哈希仍相近；
    不同 seed 的构图差异明显。这只是测试素材，与"是否 AI 合成"无关。

    返回可调用对象，同时暴露 ``row_bytes(y)`` 供整行高速生成。
    """
    kind = seed % 4
    jitter = (seed * 37 % 100) / 100.0

    def _color(t: float) -> tuple[int, int, int]:
        return (min(255, int(90 + 150 * t)),
                min(255, int(110 + 110 * t)),
                min(255, int(160 - 80 * t)))

    if kind in (0, 1):
        # t 只随行号变化：每行是常量
        def _row_value(y: int) -> float:
            v = y / height
            if kind == 0:
                return 1.0 if v < 0.42 + 0.05 * jitter else 0.3
            return 1.0 if v > 0.58 - 0.05 * jitter else 0.3

        row_cache: dict[int, bytes] = {}

        def row_bytes(y: int) -> bytes:
            if y not in row_cache:
                row_cache[y] = bytes(_color(_row_value(y))) * width
            return row_cache[y]
    elif kind == 2:
        # t 只随列号变化：所有行相同
        cols = bytearray()
        for x in range(width):
            t = 1.0 if x / width < 0.40 + 0.05 * jitter else 0.3
            cols.extend(_color(t))
        one_row = bytes(cols)

        def row_bytes(y: int) -> bytes:
            return one_row
    else:
        # 固定行内只有一个分界点（u < v/2 亮、其余暗），两段常量拼接
        bright = bytes(_color(0.85))
        dark = bytes(_color(0.3))

        def row_bytes(y: int, _h=height) -> bytes:
            # 与逐像素公式等价：(u + (1-v)/2) < 0.5  ⟺  u < v/2
            cut = int(width * (y / _h) / 2)
            return bright * cut + dark * (width - cut)

    def px(x, y):
        return tuple(row_bytes(y)[x * 3:x * 3 + 3])

    px.row_bytes = row_bytes
    return px
