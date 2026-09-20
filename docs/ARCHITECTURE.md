# 架构说明

事件溯源（event sourcing）+ 内容寻址存储（CAS）。所有结论都落在"谁、在什么条款下、
对哪个哈希版本、做了什么动作"上，因此每一件作品都能重建完整轨迹。

## 组件

```
HTTP (riverflow/api.py, 标准库 ThreadingHTTPServer)
  │  角色头校验、Idempotency-Key、JSON 边界错误
  ▼
Application (app.py) ── 装配/重放/卷宗/脱敏
  ├── UploadService        续传会话、finalize、投稿人事件       (submissions.py)
  ├── DetectionService     dHash 相似、外部信号 → 只开人工核验  (submissions.py)
  ├── TechService          技术检查 + 转码件/缩略图              (tech.py)
  ├── EditorialService     人工裁决/争议/撤稿/版面/选用/复核     (editorial.py)
  └── DistributionService  投递与回执幂等                       (distribution.py)
  │
  ├── EventStore  SQLite：event / upload_session / upload_chunk / idempotency_record
  └── BlobStore   文件系统：objects/ 与 transcodes/ 按内容哈希寻址
```

## 事件与聚合

四类聚合，各自严格递增的 `seq`：

- **submission**（投稿件）：SubmissionCreated → TechCheckRecorded →
  DetectionSignaled?/ManualReviewRequired →（FileReplaced/NoteSupplemented/
  RightsClaimRaised(RightsClaimResolved)/WithdrawalRequested/WithdrawalAccepted/
  DistributionHalted/VersionEnteredLayout）→ ManualVerdictApplied
- **review**（人工核验任务）：ReviewOpened → ReviewVerdictRecorded → ReviewClosed
- **selection**（选用单）：SelectionCreated → SelectionApproved|Rejected →
  SelectionDispatched
- **delivery**（渠道投递）：DeliveryCreated → DeliveryResultRecorded* →
  DeliveryWithdrawn?

投影（`domain.Projection`）每次从全量事件重放。征集规模（万级作品）下全量重放
仍是毫秒~百毫秒级；需要水平扩展时可改为增量投影（event 表有自增 id）。

### 为什么检测分数不进状态机

相似/搬运/合成的自动产出只有一种效果：**追加证据并开启/并入人工核验任务**。
事件载荷中显式写 `auto_conclusion: false`。聚合上的最终结论
（`ManualVerdictApplied`）只可能由 reviewer 关闭核验任务产生。
这样"分数错了"最坏后果是多一次人工，永远不会出现"模型判搬运→自动拒稿/撤稿"。

新证据到达时，投影把旧的聚合结论置空并回到 `manual_review`
（已刊发作品的版面依据不受影响，仍保留），等新的人工裁决重新汇总。

## 状态派生

投稿件状态全部由事件派生（无状态字段可被直接改写）：

- 有撤稿 → `withdrawn`
- 人工结论 copied/synthetic → `rejected`
- 有未决核验/未决争议/结论存疑/有信号未裁决 → `manual_review`
- 最新技术检查 fail → `tech_failed`
- 全部核验 authentic 且技术 pass → `verified`（唯一可被选用的状态）

## 续传与去重

1. `init` 建会话（总分片数、单片大小、可选声明哈希、视频 media_meta）。
2. `PUT chunk` 以 `(session_id, chunk_index)` 为主键，重复片返回 `stored:false`。
3. `finalize` 按序拼接 → 实算 SHA256 与声明核对 → 入 CAS →
   查"同联系方式 + 同哈希 + 在役"作品：命中则复用，不新建。
4. 会话行记录 `finalized_submission_id`，重复 finalize 直接返回同一作品。

## 内容寻址存储

- 原件不可变：`objects/ab/<sha256>`，重复字节只存一份。
- 派生件不覆盖原件：`transcodes/<源sha256>/thumb-320.ppm`、
  `.../1080p-proxy.plan.json`（真实部署中 ffmpeg worker 以同一 profile 名回写产物）。
- 视频分辨率证据：`<对象>.media.json` 侧车随原件一起入库；
  探针同时识别 mp4/mov/webm 容器头。生产替换 ffprobe 适配器时检查契约不变。

## 授权与刊发依据

- 条款冻结：`config.terms_text` 的 SHA256 为 `terms_digest`，
  SubmissionCreated 记录版本号+摘要；旧版本提交被拒。
- 选用单投递时，DeliveryCreated 复制条款版本/摘要与选用、复核人。
- `enter_layout` 写出完整依据：编辑、复核人、条款版本与摘要、当时人工结论、
  期号与版面号。撤稿只追加停发/撤回事件，**不删除**这条依据。

## 渠道回执幂等汇总

- `callback_id` 是渠道侧幂等键，同键重复回传不新增事件（返回 `deduped:true`）。
- 状态汇总为单调秩：pending(0) < failed(1) < succeeded(2) < withdrawn(3)，
  撤回后后续回调不能翻回；失败可被重试成功抬升；成功不被迟到失败打回。
- 每个渠道独立：部分失败不影响已成功渠道，队列只返回 pending/failed 渠道。

## 权限模型

内置方案用请求头表达身份（部署时放在 SSO/网关注入的信任边界之后）：

| 动作 | editor | reviewer | publisher |
|---|---|---|---|
| 选用/指定版本渠道 | ✅ | – | – |
| 人工裁决/争议裁决 | – | ✅ | – |
| 复核选用单（须与选用人不同） | – | ✅ | – |
| 受理撤稿/登记版面/投递/回执 | – | ✅(受理撤稿) | ✅ |

`admin` 拥有全部角色能力，便于迁移期运维；正式部署可在配置层移除。

## 公开面

- `/public/catalog`：至少一条 succeeded 投递的作品；字段仅署名、标题、类型、刊发记录。
- `/public/status?code=`：投稿人凭公开编号看状态，无联系方式/无内部信号细节；
  未采用作品不出现在任何公开列表。
- 时间线在卷宗内对嵌套的 contributor 做二次脱敏，凭证字段永不输出。

## 存量导入的幂等

`ingest` 以每条记录的 `legacy_id` 写入 idempotency_record；重跑整个清单
15 条记录全部跳过，作品数、回执数与队列结论不变。大文件按分片走真实续传链路，
同图改裁剪走 FileReplaced（不产生新作品），多人主张成多条独立 claim 事件。
