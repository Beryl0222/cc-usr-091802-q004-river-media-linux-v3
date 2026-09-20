"""黄河影像征集与融合报道后端的核心领域服务。

职责边界：
- 断点续传按（投稿人, 内容哈希）幂等，不重复生成作品；
- 自动检测只产生人工核验任务，不直接定性；
- 授权以提交时的条款版本留痕，替换文件不自动扩大授权；
- 选用（编辑）、复核（复核员）、发布（发布员）职责分离；
- 渠道回执按回执号幂等汇总；撤稿保留刊发依据、停止后续分发。
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from .checks import DEFAULT_LIMITS, run_checks
from .detection import detect_signals
from .storage import BlobStore

ROLE_EDITOR = "editor"
ROLE_REVIEWER = "reviewer"
ROLE_PUBLISHER = "publisher"
ROLE_CONTRIBUTOR = "contributor"

WORK_RECEIVED = "received"
WORK_SELECTED = "selected"
WORK_APPROVED = "approved"
WORK_REJECTED = "rejected"  # 仅人工复核可置为此状态

RECEIPT_STATUSES = ("success", "failure", "retracted")

_EVENT_PREFIX = {
    "work": "wk",
    "upload": "up",
    "event": "ev",
    "review": "rt",
    "publication": "pub",
    "check": "cr",
    "contributor": "ct",
    "dispute": "dp",
    "license": "lc",
}


class DomainError(Exception):
    status = 400


class NotFound(DomainError):
    status = 404


class Conflict(DomainError):
    status = 409


class Forbidden(DomainError):
    status = 403


def load_policy(path="fixtures/sample.json"):
    """读取征集配置并合并技术检查默认阈值。"""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    policy = dict(DEFAULT_LIMITS)
    policy.update(data)
    return policy


def _utcnow():
    return datetime.now(timezone.utc).isoformat()


class RiverflowService:
    def __init__(self, blob_store: BlobStore, policy: dict, now=_utcnow):
        self.blobs = blob_store
        self.policy = policy
        self._now = now
        self._seq = {}
        self.contributors = {}
        self.sessions = {}
        self.assets = {}  # content_hash -> asset
        self.works = {}
        self.events = []
        self.review_tasks = {}
        self.publications = {}  # idempotency_key -> publication
        self.disputes = {}

    # ---------- 基础 ----------

    def _next_id(self, kind):
        self._seq[kind] = self._seq.get(kind, 0) + 1
        return f"{_EVENT_PREFIX[kind]}-{self._seq[kind]:06d}"

    def _require_role(self, actor, *roles):
        if not actor or actor.get("role") not in roles:
            raise Forbidden(f"需要角色: {'/'.join(roles)}")

    def _emit(self, work_id, event_type, actor, payload):
        event = {
            "id": self._next_id("event"),
            "work_id": work_id,
            "type": event_type,
            "actor": dict(actor) if actor else None,
            "payload": payload,
            "created_at": self._now(),
        }
        self.events.append(event)
        return event

    def _get_work(self, work_id):
        work = self.works.get(work_id)
        if work is None:
            raise NotFound(f"作品不存在: {work_id}")
        return work

    # ---------- 投稿人与授权 ----------

    def register_contributor(self, name, contact):
        if not name:
            raise DomainError("投稿人姓名不能为空")
        contributor = {
            "id": self._next_id("contributor"),
            "name": name,
            "contact": contact or {},  # 联系方式绝不进入公开输出
            "created_at": self._now(),
        }
        self.contributors[contributor["id"]] = contributor
        return {"id": contributor["id"], "name": name}

    def _grant_license(self, work, terms_version, terms_text, scope, actor):
        if not terms_version or not terms_text:
            raise DomainError("授权必须关联提交时的条款版本与条款文本")
        scope = list(scope) if scope else list(self.policy["channels"])
        unknown = set(scope) - set(self.policy["channels"])
        if unknown:
            raise DomainError(f"授权范围包含未知渠道: {sorted(unknown)}")
        grant = {
            "id": self._next_id("license"),
            "work_id": work["id"],
            "terms_version": terms_version,
            "terms_sha256": hashlib.sha256(terms_text.encode("utf-8")).hexdigest(),
            "scope": scope,
            "granted_at": self._now(),
            "granted_by": dict(actor) if actor else None,
        }
        work["licenses"].append(grant)
        return grant

    # ---------- 断点续传 ----------

    def open_upload(self, contributor_id, content_hash, total_size):
        """按（投稿人, 内容哈希）幂等开启续传会话；重复调用返回同一会话。"""
        if contributor_id not in self.contributors:
            raise NotFound(f"投稿人不存在: {contributor_id}")
        if len(content_hash) != 64 or not all(c in "0123456789abcdef" for c in content_hash):
            raise DomainError("content_hash 须为 64 位小写 sha256 十六进制")
        if not isinstance(total_size, int) or total_size <= 0:
            raise DomainError("total_size 须为正整数")
        for session in self.sessions.values():
            if (
                session["contributor_id"] == contributor_id
                and session["content_hash"] == content_hash
                and session["status"] != "aborted"
            ):
                return self._session_view(session)
        session = {
            "id": self._next_id("upload"),
            "contributor_id": contributor_id,
            "content_hash": content_hash,
            "total_size": total_size,
            "chunks": {},  # offset -> bytes
            "status": "open",
            "work_id": None,
            "created_at": self._now(),
        }
        self.sessions[session["id"]] = session
        return self._session_view(session)

    def _session_view(self, session):
        received = sum(len(data) for data in session["chunks"].values())
        return {
            "id": session["id"],
            "contributor_id": session["contributor_id"],
            "content_hash": session["content_hash"],
            "total_size": session["total_size"],
            "received_bytes": received,
            "status": session["status"],
            "work_id": session["work_id"],
        }

    def upload_chunk(self, session_id, offset, data: bytes):
        session = self.sessions.get(session_id)
        if session is None:
            raise NotFound(f"上传会话不存在: {session_id}")
        if session["status"] != "open":
            raise Conflict(f"会话状态为 {session['status']}，不能继续传分片")
        if offset < 0 or offset + len(data) > session["total_size"]:
            raise DomainError("分片越界")
        session["chunks"][offset] = bytes(data)
        return self._session_view(session)

    def complete_upload(self, session_id, *, title, description, media_type, probe,
                        terms_version, terms_text, license_scope=None, actor=None):
        """完成续传并生成作品；重复完成同一会话返回同一作品，不会重复生成。"""
        session = self.sessions.get(session_id)
        if session is None:
            raise NotFound(f"上传会话不存在: {session_id}")
        if session["status"] == "complete":
            return self.get_work(session["work_id"])

        chunks = session["chunks"]
        if sum(len(d) for d in chunks.values()) != session["total_size"]:
            raise Conflict("分片未传齐，不能完成会话")
        buffer = bytearray(session["total_size"])
        for offset, data in chunks.items():
            buffer[offset:offset + len(data)] = data
        payload = bytes(buffer)
        digest = self.blobs.digest(payload)
        if digest != session["content_hash"]:
            raise Conflict(f"内容哈希校验失败: 声明 {session['content_hash']} 实得 {digest}")

        self.blobs.put_original(payload)
        asset = self.assets.get(digest)
        if asset is None:
            asset = {
                "hash": digest,
                "kind": "original",
                "media_type": media_type,
                "size": len(payload),
                "phash": (probe or {}).get("phash"),
                "probe": dict(probe or {}),
                "created_at": self._now(),
            }
            self.assets[digest] = asset

        work = {
            "id": self._next_id("work"),
            "contributor_id": session["contributor_id"],
            "title": title,
            "description": description or "",
            "media_type": media_type,
            "status": WORK_RECEIVED,
            "locked": False,
            "withdrawn": False,
            "takedown_requested": False,
            "versions": [],
            "current_version": 0,
            "licenses": [],
            "selected_by": None,
            "approved_by": None,
            "created_at": self._now(),
        }
        self.works[work["id"]] = work
        actor = actor or {"id": session["contributor_id"], "role": ROLE_CONTRIBUTOR}
        self._grant_license(work, terms_version, terms_text, license_scope, actor)
        self._emit(work["id"], "work_created", actor, {
            "upload_session_id": session["id"],
            "content_hash": digest,
            "terms_version": terms_version,
        })
        self._add_version(work, digest, note="首次提交", actor=actor)
        session["status"] = "complete"
        session["work_id"] = work["id"]
        return self.get_work(work["id"])

    # ---------- 版本、检查与风险线索 ----------

    def _add_version(self, work, content_hash, note, actor):
        asset = self.assets[content_hash]
        report = run_checks(
            report_id=self._next_id("check"),
            asset_hash=content_hash,
            media_type=work["media_type"],
            size=asset["size"],
            probe=asset["probe"],
            policy=self.policy,
            created_at=self._now(),
            text_length=self._story_length(work, content_hash),
        )
        work["versions"].append({
            "version_no": len(work["versions"]) + 1,
            "asset_hash": content_hash,
            "note": note,
            "check_report": report.to_dict(),
            "created_at": self._now(),
        })
        work["current_version"] = len(work["versions"])
        self._emit(work["id"], "checks_completed", None, {
            "version_no": work["current_version"],
            "report_id": report.id,
            "passed": report.passed,
        })
        self._open_review_tasks(work, asset)
        return report

    def _story_length(self, work, content_hash):
        if work["media_type"] != "story":
            return None
        try:
            return len(self.blobs.get(content_hash).decode("utf-8", errors="ignore"))
        except Exception:
            return None

    def _open_review_tasks(self, work, asset):
        others = []
        for other in self.works.values():
            if other["id"] == work["id"]:
                continue
            other_asset = self.assets[other["versions"][-1]["asset_hash"]]
            others.append({
                "work_id": other["id"],
                "contributor_id": other["contributor_id"],
                "content_hash": other_asset["hash"],
                "phash": other_asset.get("phash"),
            })
        signals = detect_signals(
            content_hash=asset["hash"],
            phash=asset.get("phash"),
            contributor_id=work["contributor_id"],
            probe=asset["probe"],
            others=others,
            ai_allowed=bool(self.policy.get("ai_generated_allowed")),
        )
        for signal in signals:
            task = {
                "id": self._next_id("review"),
                "work_id": work["id"],
                "kind": signal.kind,
                "score": signal.score,
                "evidence": signal.evidence,
                "status": "open",
                "resolution": None,
                "resolved_by": None,
                "created_at": self._now(),
            }
            self.review_tasks[task["id"]] = task
            self._emit(work["id"], "review_task_opened", None, {
                "task_id": task["id"],
                "kind": task["kind"],
                "score": task["score"],
            })

    def resolve_review_task(self, task_id, actor, outcome, note=""):
        """人工核验：只有复核员能关闭线索；确认违规由人定性，不由分数定性。"""
        self._require_role(actor, ROLE_REVIEWER)
        task = self.review_tasks.get(task_id)
        if task is None:
            raise NotFound(f"核验任务不存在: {task_id}")
        if task["status"] != "open":
            raise Conflict("任务已关闭")
        if outcome not in ("cleared", "confirmed_violation"):
            raise DomainError("outcome 须为 cleared 或 confirmed_violation")
        task["status"] = "closed"
        task["resolution"] = outcome
        task["resolved_by"] = dict(actor)
        task["resolved_at"] = self._now()
        work = self._get_work(task["work_id"])
        if outcome == "confirmed_violation":
            work["status"] = WORK_REJECTED
        self._emit(work["id"], "review_task_resolved", actor, {
            "task_id": task_id, "outcome": outcome, "note": note,
        })
        return dict(task)

    # ---------- 补传说明 / 替换文件 / 权属争议 / 撤稿 ----------

    def add_note(self, work_id, actor, text):
        """补传说明：作为事件留痕，不改变文件与授权。"""
        work = self._get_work(work_id)
        if not text:
            raise DomainError("说明内容不能为空")
        self._emit(work_id, "note_added", actor, {"text": text})
        return self.get_work(work_id)

    def replace_file(self, work_id, actor, data: bytes, note="", terms_version=None, terms_text=None):
        """替换文件：生成新版本、重新检查与检测；旧版本刊发依据保留。

        默认沿用提交时的授权条款；如重新征得授权则追加新的授权记录。
        已进入编辑流程的作品退回待选状态，需重新选用与复核。
        """
        work = self._get_work(work_id)
        self._require_role(actor, ROLE_CONTRIBUTOR, ROLE_EDITOR)
        if actor["role"] == ROLE_CONTRIBUTOR and actor.get("id") != work["contributor_id"]:
            raise Forbidden("只能替换本人作品的文件")
        if work["withdrawn"]:
            raise Conflict("作品已撤稿，不能替换文件")
        content_hash = self.blobs.put_original(bytes(data))
        if content_hash not in self.assets:
            self.assets[content_hash] = {
                "hash": content_hash,
                "kind": "original",
                "media_type": work["media_type"],
                "size": len(data),
                "phash": None,
                "probe": {},
                "created_at": self._now(),
            }
        previous = work["versions"][-1]["asset_hash"]
        self._add_version(work, content_hash, note=note, actor=actor)
        if terms_version and terms_text:
            self._grant_license(work, terms_version, terms_text, None, actor)
        if work["status"] in (WORK_SELECTED, WORK_APPROVED):
            work["status"] = WORK_RECEIVED
            work["selected_by"] = None
            work["approved_by"] = None
            self._emit(work_id, "progress_reset", actor, {"reason": "file_replaced"})
        self._emit(work_id, "file_replaced", actor, {
            "from_hash": previous,
            "to_hash": content_hash,
            "version_no": work["current_version"],
            "note": note,
            "terms_version": terms_version,
        })
        return self.get_work(work_id)

    def open_dispute(self, work_id, actor, claimant, detail):
        """权属争议：首次开启锁定作品，后续主张追加为同一案件的诉求事件。"""
        work = self._get_work(work_id)
        if not claimant:
            raise DomainError("主张人不能为空")
        for dispute in self.disputes.values():
            if dispute["work_id"] == work_id and dispute["status"] == "open":
                dispute["claims"].append({"claimant": claimant, "detail": detail, "at": self._now()})
                self._emit(work_id, "ownership_claim_added", actor, {
                    "dispute_id": dispute["id"], "claimant": claimant, "detail": detail,
                })
                return dict(dispute)
        dispute = {
            "id": self._next_id("dispute"),
            "work_id": work_id,
            "status": "open",
            "claims": [{"claimant": claimant, "detail": detail, "at": self._now()}],
            "created_at": self._now(),
        }
        self.disputes[dispute["id"]] = dispute
        work["locked"] = True
        self._emit(work_id, "ownership_dispute_opened", actor, {
            "dispute_id": dispute["id"], "claimant": claimant, "detail": detail,
        })
        return dict(dispute)

    def resolve_dispute(self, work_id, actor, outcome, confirmed_contributor_id=None):
        self._require_role(actor, ROLE_REVIEWER)
        work = self._get_work(work_id)
        dispute = next(
            (d for d in self.disputes.values()
             if d["work_id"] == work_id and d["status"] == "open"),
            None,
        )
        if dispute is None:
            raise NotFound("该作品没有进行中的权属争议")
        if outcome not in ("confirmed_original", "reassigned", "claims_rejected"):
            raise DomainError("outcome 须为 confirmed_original/reassigned/claims_rejected")
        if outcome == "reassigned":
            if confirmed_contributor_id not in self.contributors:
                raise DomainError("reassigned 须指定在册投稿人")
            work["contributor_id"] = confirmed_contributor_id
        dispute["status"] = "resolved"
        dispute["outcome"] = outcome
        dispute["resolved_by"] = dict(actor)
        dispute["resolved_at"] = self._now()
        work["locked"] = False
        self._emit(work_id, "ownership_dispute_resolved", actor, {
            "dispute_id": dispute["id"], "outcome": outcome,
            "confirmed_contributor_id": confirmed_contributor_id,
        })
        return dict(dispute)

    def request_takedown(self, work_id, actor, reason):
        work = self._get_work(work_id)
        if work["withdrawn"]:
            raise Conflict("作品已撤稿")
        work["takedown_requested"] = True
        self._emit(work_id, "takedown_requested", actor, {"reason": reason})
        return self.get_work(work_id)

    def execute_takedown(self, work_id, actor):
        """撤稿生效：停止后续分发；已入版面版本的刊发依据保留。"""
        self._require_role(actor, ROLE_REVIEWER)
        work = self._get_work(work_id)
        if work["withdrawn"]:
            raise Conflict("作品已撤稿")
        work["withdrawn"] = True
        for pub in self.publications.values():
            if pub["work_id"] == work_id and pub["status"] in ("sent", "failed"):
                pub["status"] = "cancelled"
                self._emit(work_id, "publication_cancelled", actor, {
                    "publication_id": pub["id"], "channel": pub["channel"],
                })
        self._emit(work_id, "takedown_executed", actor, {})
        return self.get_work(work_id)

    # ---------- 选用 / 复核 / 发布（职责分离） ----------

    def _open_tasks(self, work_id):
        return [t for t in self.review_tasks.values()
                if t["work_id"] == work_id and t["status"] == "open"]

    def _eligible(self, work):
        version = work["versions"][-1]
        return (
            version["check_report"]["passed"]
            and not self._open_tasks(work["id"])
            and work["status"] != WORK_REJECTED
            and not work["locked"]
            and not work["withdrawn"]
        )

    def select(self, work_id, actor):
        self._require_role(actor, ROLE_EDITOR)
        work = self._get_work(work_id)
        if not self._eligible(work):
            raise Conflict("作品未通过技术检查或存在待核验事项，不能选用")
        if work["status"] != WORK_RECEIVED:
            raise Conflict(f"当前状态 {work['status']} 不能选用")
        work["status"] = WORK_SELECTED
        work["selected_by"] = dict(actor)
        self._emit(work_id, "selected", actor, {})
        return self.get_work(work_id)

    def approve(self, work_id, actor):
        self._require_role(actor, ROLE_REVIEWER)
        work = self._get_work(work_id)
        if work["status"] != WORK_SELECTED:
            raise Conflict("作品未处于已选用状态")
        if work["selected_by"] and work["selected_by"].get("id") == actor.get("id"):
            raise Forbidden("选用与复核须由不同人员完成")
        work["status"] = WORK_APPROVED
        work["approved_by"] = dict(actor)
        self._emit(work_id, "approved", actor, {})
        return self.get_work(work_id)

    def publish(self, work_id, actor, channels=None):
        """发布到授权范围内渠道；按（作品, 版本, 渠道）幂等。"""
        self._require_role(actor, ROLE_PUBLISHER)
        work = self._get_work(work_id)
        if work["withdrawn"]:
            raise Conflict("作品已撤稿，停止一切分发")
        if work["locked"]:
            raise Conflict("作品存在权属争议，不能发布")
        if work["status"] != WORK_APPROVED:
            raise Conflict("作品尚未完成选用与复核")
        if not self._eligible(work):
            raise Conflict("作品当前版本未通过检查或存在待核验事项")
        license_grant = work["licenses"][-1]
        targets = list(channels) if channels else list(license_grant["scope"])
        outside = set(targets) - set(license_grant["scope"])
        if outside:
            raise Forbidden(f"超出授权范围的渠道: {sorted(outside)}")
        version_no = work["current_version"]
        results = []
        for channel in targets:
            key = f"{work_id}:v{version_no}:{channel}"
            pub = self.publications.get(key)
            if pub and pub["status"] in ("sent", "succeeded"):
                results.append(dict(pub))
                continue
            if pub and pub["status"] == "cancelled":
                raise Conflict(f"渠道 {channel} 的发布已被撤回，不能重发")
            if pub and pub["status"] == "failed":
                pub["status"] = "sent"
                self._emit(work_id, "publication_resent", actor, {
                    "publication_id": pub["id"], "channel": channel,
                })
                results.append(dict(pub))
                continue
            pub = {
                "id": self._next_id("publication"),
                "work_id": work_id,
                "version_no": version_no,
                "channel": channel,
                "idempotency_key": key,
                "status": "sent",
                "basis": {  # 刊发依据：随发布快照留存，撤稿后仍可追溯
                    "terms_version": license_grant["terms_version"],
                    "terms_sha256": license_grant["terms_sha256"],
                    "license_id": license_grant["id"],
                    "selected_by": work["selected_by"],
                    "approved_by": work["approved_by"],
                    "check_report_id": work["versions"][-1]["check_report"]["id"],
                    "asset_hash": work["versions"][-1]["asset_hash"],
                },
                "created_by": dict(actor),
                "created_at": self._now(),
                "receipts": [],
            }
            self.publications[key] = pub
            self._emit(work_id, "publication_created", actor, {
                "publication_id": pub["id"], "channel": channel, "version_no": version_no,
            })
            results.append(dict(pub))
        return results

    # ---------- 渠道回执（幂等汇总） ----------

    def record_receipt(self, *, channel, publication_key, receipt_id, status, detail=""):
        if status not in RECEIPT_STATUSES:
            raise DomainError(f"回执状态须为 {RECEIPT_STATUSES}")
        pub = self.publications.get(publication_key)
        if pub is None:
            raise NotFound(f"发布记录不存在: {publication_key}")
        if pub["channel"] != channel:
            raise Conflict("回执渠道与发布渠道不一致")
        for existing in pub["receipts"]:
            if existing["receipt_id"] == receipt_id:
                return self._receipt_view(pub, deduplicated=True)
        pub["receipts"].append({
            "receipt_id": receipt_id,
            "channel": channel,
            "status": status,
            "detail": detail,
            "recorded_at": self._now(),
        })
        if pub["status"] != "cancelled":
            pub["status"] = {"success": "succeeded", "failure": "failed",
                             "retracted": "retracted"}[status]
        self._emit(pub["work_id"], "receipt_recorded", None, {
            "publication_id": pub["id"], "channel": channel,
            "receipt_id": receipt_id, "status": status,
        })
        return self._receipt_view(pub, deduplicated=False)

    def _receipt_view(self, pub, deduplicated):
        return {
            "publication_id": pub["id"],
            "idempotency_key": pub["idempotency_key"],
            "channel": pub["channel"],
            "status": pub["status"],
            "receipts_count": len(pub["receipts"]),
            "deduplicated": deduplicated,
        }

    # ---------- 查询 ----------

    def get_work(self, work_id):
        work = self._get_work(work_id)
        return {
            "id": work["id"],
            "contributor_id": work["contributor_id"],
            "title": work["title"],
            "media_type": work["media_type"],
            "status": work["status"],
            "locked": work["locked"],
            "withdrawn": work["withdrawn"],
            "takedown_requested": work["takedown_requested"],
            "current_version": work["current_version"],
            "open_review_tasks": len(self._open_tasks(work_id)),
            "checks_passed": work["versions"][-1]["check_report"]["passed"],
        }

    def publishable_queue(self):
        """可发布队列：已完成选用与复核、无争议未撤稿、检查通过的作品。"""
        queue = []
        for work in self.works.values():
            if work["status"] != WORK_APPROVED or not self._eligible(work):
                continue
            pubs = [p for p in self.publications.values() if p["work_id"] == work["id"]]
            queue.append({
                "work_id": work["id"],
                "title": work["title"],
                "media_type": work["media_type"],
                "version_no": work["current_version"],
                "licensed_channels": list(work["licenses"][-1]["scope"]),
                "channels": {p["channel"]: p["status"] for p in pubs},
            })
        return queue

    def provenance(self, work_id):
        """逐件溯源：来源、授权版本、处理轨迹、传播去向。"""
        work = self._get_work(work_id)
        contributor = self.contributors[work["contributor_id"]]
        sessions = [s for s in self.sessions.values() if s.get("work_id") == work_id]
        pubs = [p for p in self.publications.values() if p["work_id"] == work_id]
        return {
            "work_id": work_id,
            "title": work["title"],
            "status": work["status"],
            "locked": work["locked"],
            "withdrawn": work["withdrawn"],
            "source": {
                "contributor_id": contributor["id"],
                "contributor_name": contributor["name"],
                "contact": dict(contributor["contact"]),  # 仅供编辑部内部
                "upload_sessions": [self._session_view(s) for s in sessions],
            },
            "licenses": [dict(g) for g in work["licenses"]],
            "versions": [dict(v) for v in work["versions"]],
            "review_tasks": [dict(t) for t in self.review_tasks.values()
                             if t["work_id"] == work_id],
            "events": [e for e in self.events if e["work_id"] == work_id],
            "distribution": [{
                "publication_id": p["id"],
                "channel": p["channel"],
                "version_no": p["version_no"],
                "status": p["status"],
                "basis": dict(p["basis"]),
                "receipts": list(p["receipts"]),
            } for p in pubs],
        }

    def public_works(self):
        """公开查询：仅已采用且未撤稿的作品，不含联系方式与未采用素材。"""
        items = []
        for work in self.works.values():
            if work["withdrawn"]:
                continue
            succeeded = [p for p in self.publications.values()
                         if p["work_id"] == work["id"] and p["status"] == "succeeded"]
            if not succeeded:
                continue
            contributor = self.contributors[work["contributor_id"]]
            items.append({
                "work_id": work["id"],
                "title": work["title"],
                "media_type": work["media_type"],
                "author": contributor["name"],
                "channels": sorted({p["channel"] for p in succeeded}),
                "published_version": max(p["version_no"] for p in succeeded),
            })
        return items
