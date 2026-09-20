"""技术检查与转码件管理。

检查项全部给出"期望值/实测值/证据"，可逐条复核；自动流程只记录
pass/fail/manual 三态，其中主题契合度、原创性、合成嫌疑永远是 manual，
不允许由分数给出结论。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from . import media
from .domain import TECH_CHECK_RECORDED


class TechService:
    def __init__(self, app):
        self.app = app

    def run_checks(self, sid: str, digest: str) -> dict[str, Any]:
        s = self.app.proj.submissions[sid]
        path = self.app.blobs.object_path(digest)
        info = media.probe(path, s.media_type)
        checks: list[dict[str, Any]] = self._checks_for(s.media_type, info, path)
        # 主题与非商业属性不是文件可判定项：显式留给人工与条款，不冒充自动结论
        checks.append({
            "check_id": "theme-fit",
            "name": "黄河主题契合度",
            "result": "manual",
            "expected": "内容与「我家门前有条河」黄河主题相关",
            "actual": None,
            "evidence": {"note": "主题判断在编辑复核环节完成，自动检查不下结论"},
        })
        overall = "pass" if all(c["result"] != "fail" for c in checks) else "fail"
        record = {"sha256": digest, "overall": overall,
                  "checks": checks, "media_info": _info_dict(info)}
        with self.app.store.tx() as conn:
            self.app.store.append(
                conn, "submission", sid, TECH_CHECK_RECORDED, "system:tech", record)
        self.app.reload()
        self._create_derivatives(
            self.app.proj.submissions[sid], digest, info)
        return record

    def _checks_for(self, kind: str, info: media.MediaInfo, path: Path):
        if kind == "image":
            long_edge = max(filter(None, (info.width, info.height)), default=None)
            return [
                _check("format-recognized", "文件格式可识别",
                       info.kind == "image", "image", info.container),
                _check("image-resolution", "长边像素",
                       (long_edge or 0) >= media.MIN_IMAGE_LONG_EDGE,
                       f">= {media.MIN_IMAGE_LONG_EDGE}px", long_edge),
                _check("fingerprint", "感知指纹可计算（相似排查用）",
                       True, "dhash64",
                       "computable" if media.dhash_fingerprint(path) is not None
                       else "需人工目检（该格式零依赖解码器不支持）",
                       result_override=("pass" if media.dhash_fingerprint(path) is not None
                                        else "manual")),
            ]
        if kind == "video":
            ok_meta = info.height is not None and info.duration_seconds is not None
            return [
                _check("video-container", "视频容器可识别",
                       info.container is not None, "mp4/mov/webm", info.container),
                _check("video-resolution", "垂直分辨率不低于 1080P",
                       (info.height or 0) >= media.MIN_VIDEO_HEIGHT,
                       f">= {media.MIN_VIDEO_HEIGHT}", info.height),
                _check("video-duration", f"时长 {media.MIN_VIDEO_SECONDS}-"
                       f"{media.MAX_VIDEO_SECONDS} 秒",
                       ok_meta and media.MIN_VIDEO_SECONDS
                       <= (info.duration_seconds or 0) <= media.MAX_VIDEO_SECONDS,
                       f"{media.MIN_VIDEO_SECONDS}-{media.MAX_VIDEO_SECONDS}s",
                       info.duration_seconds),
                _check("video-metadata", "分辨率/时长元数据可供复核",
                       ok_meta, "sidecar media.json 或可解析容器",
                       "present" if ok_meta else "; ".join(info.notes) or "missing"),
            ]
        if kind == "story":
            text = path.read_text(encoding="utf-8", errors="replace")
            n = len(text)
            return [
                _check("story-encoding", "UTF-8 文本可解码",
                       "UTF-8 解码失败" not in " ".join(info.notes),
                       "utf-8", "ok" if not info.notes else info.notes[0]),
                _check("story-length", "正文字数",
                       media.MIN_STORY_CHARS <= n <= media.MAX_STORY_CHARS,
                       f"{media.MIN_STORY_CHARS}-{media.MAX_STORY_CHARS} 字", n),
            ]
        return [_check("format-recognized", "文件格式可识别", False,
                       "image/video/story", "unknown")]

    def _create_derivatives(self, s, digest: str, info: media.MediaInfo) -> None:
        """按原件哈希组织转码/派生件；可重复生成，不触碰原件。"""
        if s.media_type == "image":
            thumb = _thumbnail_ppm(self.app.blobs.object_path(digest), 320)
            if thumb is not None:
                self.app.blobs.put_derivative(digest, "thumb-320.ppm", thumb)
        elif s.media_type == "video":
            plan = {
                "source_sha256": digest,
                "profiles": [{
                    "profile": "1080p-proxy",
                    "video_height": 1080,
                    "video_codec": "h264",
                    "keep_container": info.container,
                }],
                "note": "转码工作单：ffmpeg worker 完成后以同 profile 名回写产物",
            }
            self.app.blobs.put_derivative(
                digest, "1080p-proxy.plan.json",
                json.dumps(plan, ensure_ascii=False, indent=2).encode("utf-8"))


def _check(check_id: str, name: str, passed: bool, expected: Any, actual: Any,
           result_override: str | None = None) -> dict[str, Any]:
    return {
        "check_id": check_id,
        "name": name,
        "result": result_override or ("pass" if passed else "fail"),
        "expected": expected,
        "actual": actual,
        "evidence": {"method": "automated-tech-check"},
    }


def _info_dict(info: media.MediaInfo) -> dict[str, Any]:
    return {
        "kind": info.kind, "width": info.width, "height": info.height,
        "duration_seconds": info.duration_seconds, "has_audio": info.has_audio,
        "container": info.container, "codec": info.codec, "notes": info.notes,
    }


def _thumbnail_ppm(path: Path, long_edge: int) -> bytes | None:
    """零依赖缩略图：最近邻缩放并写成 PPM(P6)。无法解码返回 None。"""
    decoded = media.decode_rgb(path)
    if decoded is None:
        return None
    w, h, rgb = decoded
    scale = min(1.0, long_edge / max(w, h))
    nw, nh = max(1, round(w * scale)), max(1, round(h * scale))
    out = bytearray(nw * nh * 3)
    for y in range(nh):
        sy = min(h - 1, int(y / scale))
        for x in range(nw):
            sx = min(w - 1, int(x / scale))
            src_i = (sy * w + sx) * 3
            dst_i = (y * nw + x) * 3
            out[dst_i:dst_i + 3] = rgb[src_i:src_i + 3]
    header = f"P6\n{nw} {nh}\n255\n".encode("ascii")
    return header + bytes(out)
