"""征集活动配置与投稿条款版本。

条款一旦发布即冻结：投稿事件必须记录提交时的条款版本与全文摘要，
后续条款更新不会改变既有授权依据（"授权关联提交时条款"）。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

TERMS_TEXT = """\
「我家门前有条河」影像征集授权条款（提交即同意）

一、投稿人保证作品为本人原创或已取得完整授权，不含搬运、抄袭内容，
    非 AI 合成或在人工创作基础上仅作轻度辅助处理；如系多人合作作品，
    须在投稿说明中列明全部权利人。
二、投稿人授予主办方在本次征集活动相关范围内，以非商业方式使用作品，
    包括但不限于：活动页展示、纸质报纸刊发、官方新媒体账号发布、
    评审与宣传报道。授权不得转用于纯商业广告。
三、主办方有权为适配不同渠道对作品做必要的压缩、裁剪与转码，
    不得歪曲作品主题；原件始终按内容哈希留存。
四、投稿人可申请补传说明、替换文件；对权属争议，任何一方均可提出主张，
    作品进入人工核验，争议解决前不得刊发。
五、投稿人可随时申请撤稿。已进入版面（报纸/活动页/新媒体）的版本，
    主办方保留刊发依据记录，同时停止一切后续分发并尽量召回已发布内容。
六、主办方仅在征集、核验与联络所必需范围内保存联系方式，
    公开查询不展示联系方式，也不展示未采用素材。
"""

# 活动只接受非商业属性的使用，商业渠道在系统层面不可登记。
COMMERCIAL_USE_ALLOWED = False


@dataclass(frozen=True)
class CampaignConfig:
    campaign_id: str
    accepted_media: tuple[str, ...]
    minimum_video_height: int
    channels: tuple[str, ...]
    ai_generated_allowed: bool
    terms_version: str
    terms_text: str

    @property
    def terms_digest(self) -> str:
        return hashlib.sha256(self.terms_text.encode("utf-8")).hexdigest()

    @classmethod
    def load(cls, path: str | Path) -> "CampaignConfig":
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls(
            campaign_id=raw["campaign_id"],
            accepted_media=tuple(raw["accepted_media"]),
            minimum_video_height=int(raw["minimum_video_height"]),
            channels=tuple(raw["channels"]),
            ai_generated_allowed=bool(raw["ai_generated_allowed"]),
            terms_version=raw.get("terms_version", "terms-2026-01"),
            terms_text=TERMS_TEXT,
        )
