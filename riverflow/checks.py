"""技术检查：1080P 等硬性指标输出可复核报告。

每份报告记录规则版本、每项检查的实测值与阈值，编辑与复核员
可以据此独立重算同一结论，而不是只看到一个总分。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

RULESET_VERSION = "tech-rules/2026-09-01"

VIDEO_CODECS = ("av1", "h264", "hevc", "prores", "vp9")
IMAGE_FORMATS = ("jpeg", "jpg", "png", "tif", "tiff")

DEFAULT_LIMITS = {
    "min_image_width": 1440,
    "min_image_height": 1080,
    "min_story_chars": 50,
    "max_bytes": 4 * 1024 ** 3,
}


@dataclass
class CheckResult:
    name: str
    passed: bool
    measured: object
    expected: str
    detail: str = ""

    def to_dict(self):
        return asdict(self)


@dataclass
class CheckReport:
    id: str
    asset_hash: str
    media_type: str
    ruleset_version: str
    results: list
    passed: bool
    created_at: str

    def to_dict(self):
        data = asdict(self)
        data["results"] = [r.to_dict() for r in self.results]
        return data


def run_checks(*, report_id, asset_hash, media_type, size, probe, policy, created_at, text_length=None):
    """对单个资产执行技术检查，返回可复核报告。

    probe 为上线管线（ffprobe/identify 等）提取的元数据；story 类
    直接以正文长度作为实测值。所有判定只依赖入参，结果可复算。
    """
    results = []

    def add(name, passed, measured, expected, detail=""):
        results.append(CheckResult(name, bool(passed), measured, expected, detail))

    accepted = policy.get("accepted_media", [])
    add("media_type_accepted", media_type in accepted, media_type, f"one of {accepted}")
    max_bytes = policy["max_bytes"]
    add("size_within_limit", 0 < size <= max_bytes, size, f"1..{max_bytes}")

    if media_type == "video":
        height = probe.get("height")
        minimum = policy["minimum_video_height"]
        add(
            "min_height",
            isinstance(height, int) and height >= minimum,
            height,
            f">= {minimum}",
            "" if isinstance(height, int) else "缺少高度元数据",
        )
        width = probe.get("width")
        add("width_present", isinstance(width, int) and width > 0, width, "> 0")
        duration = probe.get("duration_sec")
        add(
            "duration_present",
            isinstance(duration, (int, float)) and duration > 0,
            duration,
            "> 0",
        )
        codec = str(probe.get("codec") or "").lower()
        add("codec_supported", codec in VIDEO_CODECS, codec or None, f"one of {list(VIDEO_CODECS)}")
    elif media_type == "image":
        height = probe.get("height")
        add(
            "min_height",
            isinstance(height, int) and height >= policy["min_image_height"],
            height,
            f">= {policy['min_image_height']}",
        )
        width = probe.get("width")
        add(
            "min_width",
            isinstance(width, int) and width >= policy["min_image_width"],
            width,
            f">= {policy['min_image_width']}",
        )
        fmt = str(probe.get("format") or "").lower()
        add("format_supported", fmt in IMAGE_FORMATS, fmt or None, f"one of {list(IMAGE_FORMATS)}")
    elif media_type == "story":
        length = text_length if text_length is not None else probe.get("text_length")
        add(
            "min_length",
            isinstance(length, int) and length >= policy["min_story_chars"],
            length,
            f">= {policy['min_story_chars']}",
        )

    return CheckReport(
        id=report_id,
        asset_hash=asset_hash,
        media_type=media_type,
        ruleset_version=RULESET_VERSION,
        results=results,
        passed=all(r.passed for r in results),
        created_at=created_at,
    )
