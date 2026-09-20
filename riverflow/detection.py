"""风险线索检测：只产出线索，不直接定性。

相似作品、疑似搬运、疑似合成内容一律转为人工核验任务；
检测分数只用于排序与提示，不能作为退稿或判重的依据。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

HAMMING_THRESHOLD = 10  # 64 位感知哈希，汉明距离 ≤10 视为相似线索

AI_MARKERS = (
    "ai-generated",
    "dall-e",
    "dalle",
    "diffusion",
    "midjourney",
    "stable diffusion",
)


@dataclass
class RiskSignal:
    kind: str  # duplicate | similar | suspected_repost | suspected_synthetic
    score: float
    evidence: dict

    def to_dict(self):
        return asdict(self)


def hamming_distance(hex_a, hex_b):
    """两个十六进制感知哈希的汉明距离；长度不一致返回 None。"""
    try:
        a = int(hex_a, 16)
        b = int(hex_b, 16)
    except (TypeError, ValueError):
        return None
    if len(str(hex_a).strip()) != len(str(hex_b).strip()):
        return None
    return bin(a ^ b).count("1")


def detect_signals(*, content_hash, phash, contributor_id, probe, others, ai_allowed):
    """比对在库作品，返回风险线索列表。

    others: [{"work_id", "contributor_id", "content_hash", "phash"}]
    """
    signals = []
    for other in others:
        if other["content_hash"] == content_hash:
            kind = "duplicate" if other["contributor_id"] == contributor_id else "suspected_repost"
            signals.append(
                RiskSignal(kind, 1.0, {"other_work_id": other["work_id"], "match": "exact_hash"})
            )
            continue
        distance = hamming_distance(phash, other.get("phash")) if phash else None
        if distance is not None and distance <= HAMMING_THRESHOLD:
            kind = "similar" if other["contributor_id"] == contributor_id else "suspected_repost"
            signals.append(
                RiskSignal(
                    kind,
                    round(1.0 - distance / 64.0, 4),
                    {"other_work_id": other["work_id"], "hamming_distance": distance},
                )
            )

    generator = str(probe.get("generator") or "").lower()
    if generator and any(marker in generator for marker in AI_MARKERS):
        signals.append(
            RiskSignal(
                "suspected_synthetic",
                0.9,
                {"generator": probe["generator"], "ai_generated_allowed": ai_allowed},
            )
        )
    probability = probe.get("synthetic_probability")
    if isinstance(probability, (int, float)) and probability >= 0.5:
        signals.append(
            RiskSignal(
                "suspected_synthetic",
                float(probability),
                {"synthetic_probability": probability, "ai_generated_allowed": ai_allowed},
            )
        )
    return signals
