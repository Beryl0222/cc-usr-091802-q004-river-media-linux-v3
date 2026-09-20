"""媒体技术探针与感知指纹。

零第三方依赖实现：PNG（8 位灰度/RGB）、BMP、PNM 的尺寸与像素解码，
JPEG 尺寸解析；视频容器识别（mp4/mov/webm）配合 ``<文件>.media.json``
侧车元数据给出分辨率/时长（生产环境可替换为 ffprobe 适配器，契约不变）。

所有自动产出都只是*可复核的技术结果与信号*：
``dhash_fingerprint`` 用于相似作品线索，不做原创与否的结论。
"""

from __future__ import annotations

import json
import struct
import zlib
from dataclasses import dataclass, field
from pathlib import Path

# ---- 技术门槛（与 fixtures/sample.json 对齐） -----------------------------

MIN_VIDEO_HEIGHT = 1080
MIN_VIDEO_SECONDS = 3
MAX_VIDEO_SECONDS = 600
MIN_IMAGE_LONG_EDGE = 1600
MIN_STORY_CHARS = 10
MAX_STORY_CHARS = 5000
# 感知哈希汉明距离阈值：低于该值只生成"相似线索"，结论留给人工。
# 取 10 为 dHash64 常用的"疑似同源"上限；宁多报转人工，不自动漏放。
DHASH_REVIEW_DISTANCE = 10


@dataclass
class MediaInfo:
    kind: str  # image | video | story | unknown
    width: int | None = None
    height: int | None = None
    duration_seconds: float | None = None
    has_audio: bool | None = None
    container: str | None = None
    codec: str | None = None
    notes: list[str] = field(default_factory=list)


# ---- 签名与尺寸 -----------------------------------------------------------


def probe(path: str | Path, media_type: str) -> MediaInfo:
    path = Path(path)
    if media_type == "story":
        text = path.read_bytes()
        try:
            text.decode("utf-8")
        except UnicodeDecodeError as exc:
            return MediaInfo("story", notes=[f"UTF-8 解码失败: {exc}"])
        return MediaInfo("story", notes=[f"字数 {len(text.decode('utf-8'))}"])

    head = path.read_bytes()[:32]
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        w, h = struct.unpack(">II", head[16:24])
        return MediaInfo("image", w, h, container="png")
    if head[:3] == b"\xff\xd8\xff":
        w, h = _jpeg_size(path)
        return MediaInfo("image", w, h, container="jpeg")
    if head[:6] in (b"GIF87a", b"GIF89a"):
        w, h = struct.unpack("<HH", head[6:10])
        return MediaInfo("image", w, h, container="gif")
    if head[:2] == b"BM":
        w, h = _bmp_size(head, path)
        return MediaInfo("image", w, h, container="bmp")
    if head[:2] in (b"P5", b"P6"):
        info = _pnm_info(path)
        return MediaInfo("image", info[0], info[1], container="pnm")
    if media_type == "video":
        return _probe_video(path, head)
    return MediaInfo("unknown", notes=["无法识别的文件格式"])


def _jpeg_size(path: Path) -> tuple[int | None, int | None]:
    data = path.read_bytes()
    i = 2
    while i + 9 < len(data):
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        i += 2
        if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
            continue
        if i + 2 >= len(data):
            break
        seg_len = struct.unpack(">H", data[i:i + 2])[0]
        if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                      0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
            h, w = struct.unpack(">HH", data[i + 3:i + 7])
            return w, h
        i += seg_len
    return None, None


def _bmp_size(head: bytes, path: Path) -> tuple[int | None, int | None]:
    data = path.read_bytes()[:32]
    if len(data) >= 26:
        w = struct.unpack("<i", data[18:22])[0]
        h = abs(struct.unpack("<i", data[22:26])[0])
        return w, h
    return None, None


def _pnm_info(path: Path) -> tuple[int, int]:
    raw = path.read_bytes()
    tokens = _pnm_tokens(raw)
    # magic, width, height, maxval
    return int(tokens[1]), int(tokens[2])


def _pnm_tokens(raw: bytes) -> list[bytes]:
    out: list[bytes] = []
    i, n = 0, len(raw)

    def skip_ws_comments():
        nonlocal i
        while i < n:
            if raw[i] in b" \t\n\r":
                i += 1
            elif raw[i] == ord("#"):
                while i < n and raw[i] not in b"\n\r":
                    i += 1
            else:
                break

    for _ in range(4):
        skip_ws_comments()
        start = i
        while i < n and raw[i] not in b" \t\n\r":
            i += 1
        out.append(raw[start:i])
    return out


def _probe_video(path: Path, head: bytes) -> MediaInfo:
    container = None
    if head[4:8] == b"ftyp":
        brand = head[8:12].decode("latin-1", "replace")
        container = {"isom": "mp4", "mp42": "mp4", "qt  ": "mov"}.get(brand, f"mp4({brand})")
    elif head[:4] == b"\x1aE\xdf\xa3":
        container = "webm"
    sidecar = Path(str(path) + ".media.json")
    if sidecar.is_file():
        meta = json.loads(sidecar.read_text(encoding="utf-8"))
        return MediaInfo(
            "video",
            int(meta["width"]), int(meta["height"]),
            float(meta.get("duration_seconds", 0)) or None,
            bool(meta.get("has_audio")), container or meta.get("container"),
            meta.get("codec"),
        )
    notes = ["缺少侧车元数据，分辨率无法复核"] if container in ("mp4", "mov", "webm") \
        else ["无法识别的视频容器，且缺少侧车元数据"]
    return MediaInfo("video", container=container, notes=notes)


# ---- 像素解码与差异哈希 ----------------------------------------------------


def grayscale_grid(path: str | Path, cols: int = 9, rows: int = 8) -> list[int] | None:
    """把可解码图像缩放到 cols×rows 灰度网格；无法解码时返回 None。

    返回 None 表示"无法计算指纹"，该作品的相似性只能转人工，不能当作无风险。
    """
    decoded = decode_rgb(path)
    if decoded is None:
        return None
    w, h, rgb = decoded
    grid: list[int] = []
    for oy in range(rows):
        for ox in range(cols):
            sx = min(w - 1, (ox * w) // cols)
            sy = min(h - 1, (oy * h) // rows)
            i = (sy * w + sx) * 3
            r, g, b = rgb[i], rgb[i + 1], rgb[i + 2]
            grid.append((r * 77 + g * 150 + b * 29) >> 8)
    return grid


def decode_rgb(path: str | Path) -> tuple[int, int, bytes] | None:
    """解码支持的图像为交错 RGB 字节；不支持时返回 None（交人工，不猜）。"""
    path = Path(path)
    head = path.read_bytes()[:8]
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return _png_rgb(path)
    if head[:2] == b"BM":
        return _bmp_rgb(path)
    if head[:2] in (b"P5", b"P6"):
        return _pnm_rgb(path)
    return None


def dhash_fingerprint(path: str | Path) -> int | None:
    grid = grayscale_grid(path, 9, 8)
    if grid is None:
        return None
    bits = 0
    for row in range(8):
        for col in range(8):
            bits <<= 1
            if grid[row * 9 + col] > grid[row * 9 + col + 1]:
                bits |= 1
    return bits


def hamming_distance(a: int, b: int) -> int:
    return (a ^ b).bit_count()


def _png_rgb(path: Path):
    data = path.read_bytes()
    pos = 8
    width = height = bit_depth = color_type = None
    idat = b""
    while pos + 8 <= len(data):
        length, ctype = struct.unpack(">I4s", data[pos:pos + 8])
        chunk = data[pos + 8:pos + 8 + length]
        pos += 12 + length
        if ctype == b"IHDR":
            width, height, bit_depth, color_type = struct.unpack(">IIBB", chunk[:10])
        elif ctype == b"IDAT":
            idat += chunk
        elif ctype == b"IEND":
            break
    # 零依赖版支持 8 位灰度/RGB/RGBA；其余格式生产环境由 libpng 适配器处理
    if bit_depth != 8 or color_type not in (0, 2, 6):
        return None
    bpp = {0: 1, 2: 3, 6: 4}[color_type]
    raw = zlib.decompress(idat)
    stride = width * bpp
    # 快速路径：全部扫描行都是 filter 0（本系统与常见编码器的常见输出），
    # 用 C 级切片抽取像素，避免逐像素 Python 重建。
    filter_types = raw[0::stride + 1]
    if set(filter_types) == {0}:
        recon = bytearray(stride * height)
        view = memoryview(raw)
        for y in range(height):
            start = y * (stride + 1) + 1
            recon[y * stride:(y + 1) * stride] = view[start:start + stride]
    else:
        recon = _png_reconstruct(raw, width, height, stride, bpp)
    rgb = bytearray(width * height * 3)
    if color_type == 0:
        g = bytes(recon)
        rgb[0::3] = g
        rgb[1::3] = g
        rgb[2::3] = g
        return width, height, bytes(rgb)
    if color_type == 2:
        return width, height, bytes(recon)
    for p_ in range(width * height):
        r, g, b, a = recon[p_ * 4:p_ * 4 + 4]
        # 直接合成到白底，避免透明区域指纹抖动
        rgb[p_ * 3] = (r * a + 255 * (255 - a)) // 255
        rgb[p_ * 3 + 1] = (g * a + 255 * (255 - a)) // 255
        rgb[p_ * 3 + 2] = (b * a + 255 * (255 - a)) // 255
    return width, height, bytes(rgb)


def _png_reconstruct(raw: bytes, width: int, height: int,
                     stride: int, bpp: int) -> bytearray:
    """含非零过滤类型时的完整 PNG 行重建（慢路径，真实图片走这里）。"""
    recon = bytearray()
    prev = bytearray(stride)
    i = 0
    for _ in range(height):
        ftype = raw[i]
        line = bytearray(raw[i + 1:i + 1 + stride])
        i += 1 + stride
        for x in range(stride):
            left = line[x - bpp] if x >= bpp else 0
            up = prev[x]
            ul = prev[x - bpp] if x >= bpp else 0
            if ftype == 1:
                line[x] = (line[x] + left) & 0xFF
            elif ftype == 2:
                line[x] = (line[x] + up) & 0xFF
            elif ftype == 3:
                line[x] = (line[x] + ((left + up) // 2)) & 0xFF
            elif ftype == 4:
                line[x] = (line[x] + _paeth(left, up, ul)) & 0xFF
        recon.extend(line)
        prev = line
    return recon


def _paeth(a: int, b: int, c: int) -> int:
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    if pb <= pc:
        return b
    return c


def _bmp_rgb(path: Path):
    data = path.read_bytes()
    if len(data) < 54:
        return None
    offset = struct.unpack("<I", data[10:14])[0]
    width = struct.unpack("<i", data[18:22])[0]
    height_signed = struct.unpack("<i", data[22:26])[0]
    height = abs(height_signed)
    top_down = height_signed < 0
    bpp = struct.unpack("<H", data[28:30])[0]
    compression = struct.unpack("<I", data[30:34])[0]
    if compression != 0 or bpp not in (24, 32, 8):
        return None
    palette = []
    if bpp == 8:
        pal_size = struct.unpack("<I", data[14:18])[0] or 1024
        pal_start = 14 + pal_size
        for i in range(256):
            base = pal_start + i * 4
            if base + 3 > len(data):
                return None
            b, g, r = data[base:base + 3]
            palette.append((r, g, b))
    row_size = ((bpp * width + 31) // 32) * 4
    rgb = bytearray(width * height * 3)
    for y in range(height):
        src_y = y if top_down else height - 1 - y
        row_start = offset + src_y * row_size
        for x in range(width):
            if bpp == 8:
                r, g, b = palette[data[row_start + x]]
            else:
                px = row_start + x * (bpp // 8)
                b, g, r = data[px], data[px + 1], data[px + 2]
            i = (y * width + x) * 3
            rgb[i], rgb[i + 1], rgb[i + 2] = r, g, b
    return width, height, bytes(rgb)


def _pnm_rgb(path: Path):
    raw = path.read_bytes()
    magic = raw[:2]
    if magic not in (b"P5", b"P6"):
        return None
    tokens = _pnm_tokens(raw)
    width, height, maxval = int(tokens[1]), int(tokens[2]), int(tokens[3])
    if maxval > 255:
        return None
    start = 0
    count = 4
    for _ in range(count):
        while start < len(raw) and raw[start] in b" \t\n\r#":
            if raw[start] == ord("#"):
                while start < len(raw) and raw[start] not in b"\n\r":
                    start += 1
            start += 1
        while start < len(raw) and raw[start] not in b" \t\n\r":
            start += 1
    start += 1
    raster = raw[start:start + width * height * (1 if magic == b"P5" else 3)]
    if magic == b"P6":
        return width, height, raster
    rgb = bytearray()
    for g_ in raster:
        rgb.extend((g_, g_, g_))
    return width, height, bytes(rgb)
