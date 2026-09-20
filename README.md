# 黄河影像征集与融合报道后端

「我家门前有条河」影像征集的**收稿—核验—选用—刊发**一体化后端。仅用 Python 标准库
（+ SQLite，Python 自带）实现，无额外运行时依赖，`pip install pytest` 后即可验证。

它解决邮箱收稿分不清的三件事：**哪个版本是原件、哪条结论是人做的、哪一版进了哪个渠道**。

## 它如何回应需求

| 编辑部诉求 | 系统保证 |
|---|---|
| 断点续传不重复生成作品 | 分片按 `(会话,序号)` 去重；complete/finalize 幂等；同投稿人同内容哈希在役作品自动合并 |
| 以内容哈希组织原件与转码件 | 原件 `storage/objects/<前2位>/<sha256>`；转码件 `storage/transcodes/<源哈希>/<档位>.bin`；视频侧车元数据随原件存放 |
| 1080P 等技术检查可复核 | 每项给出 `expected / actual / evidence`；视频高度 ≥1080、时长、容器、感知指纹可算性逐项判定；主题与原创性恒为 `manual` |
| 相似/搬运/合成只能人工定性 | 检测产出一律是**信号（含分数与证据）+ 人工核验任务**；状态只能是 `manual_review`，结论只由复核岗写入 |
| 授权关联提交时条款 | 投稿事件冻结 `terms_version + 全文 sha256 摘要`；旧条款版本不能提交；刊发依据记录同一份条款 |
| 补说明/替换/争议/撤稿各自成事件 | `NoteSupplemented / FileReplaced / RightsClaimRaised / WithdrawalRequested` 等只增事件 |
| 已进入版面版本保留依据、停后续分发 | `VersionEnteredLayout` 冻结选用人/复核人/条款；受理撤稿写 `DistributionHalted`，已成功渠道等撤回回执，依据不删 |
| 选用/复核/发布职责分离 | `editor / reviewer / publisher` 三角色服务端强制；同一人不能自选用自复核 |
| 渠道回执幂等汇总 | 以渠道侧 `callback_id` 去重；部分失败可重试，迟到失败不覆盖成功，撤回为终态 |
| 公开查询不暴露隐私与未采用素材 | `/public/*` 只列**有成功刊发**的作品，无联系方式；投稿人凭公开编号自查同样脱敏 |
| 逐件说明来源/授权/轨迹/去向 | 员工可调取**卷宗 dossier**：来源、授权版本、全部版本、技术结果、信号、人工结论、选用、版面、各渠道回执、脱敏时间线 |

## 快速开始

```bash
python3 service.py --check                 # 自检
python3 service.py serve --port 8000       # 启动 API
python3 service.py terms                   # 查看当前投稿条款
python3 service.py ingest fixtures/legacy_manifest.json --data-dir ./data
# 导入邮箱时代的存量记录，结束后输出可发布队列
python3 -m pytest                          # 62 个测试
python3 -m unittest -v                     # 基线契约保持兼容
```

## 作品生命周期

```
POST /v1/uploads → PUT chunks（可断点重发）→ POST .../finalize
      │  分片去重、内容哈希校验、同人同内容合并
      ▼
 TechCheckRecorded（1080P/清晰度/格式，逐项证据）
      ├── fail ──▶ tech_failed（退回替换文件，不占人工）
      └── pass ──▶ ManualReviewRequired（原创性+主题，每件必审）
                         ▲ 相似/搬运/合成信号只能追加到这里，不能定性
                         ▼
              reviewer 记录人工结论 authentic/copied/synthetic/inconclusive
                         ▼
      editor 选用（指定版本+非商业渠道）→ reviewer 复核（不得同人）
                         ▼
      publisher 投递 → 各渠道 callback_id 回执（成功/失败/撤回，幂等）
                         ▼
      VersionEnteredLayout（报纸版面冻结刊发依据）
撤稿：WithdrawalRequested → 受理 → DistributionHalted → 渠道撤回（依据保留）
```

## 主要 HTTP 接口

员工接口需请求头 `X-Staff-Id` + `X-Staff-Role`；写操作可带 `Idempotency-Key`。

- `POST /v1/uploads`、`PUT /v1/uploads/{sid}/chunks/{i}`、`POST /v1/uploads/{sid}/finalize`
- `POST /v1/submissions/{id}/notes | /replace | /withdraw | /rights-claims`
- `POST /v1/staff/detections`（录入外部模型/搜索线索，**只进人工**）
- `POST /v1/staff/reviews/{rid}/verdict`（reviewer 定性的唯一入口）
- `POST /v1/staff/selections` → `.../approve|reject` → `.../dispatch`
- `POST /v1/staff/deliveries/{id}/result|withdraw|layout`
- `GET /v1/staff/inbox`、`/v1/staff/reviews/open`、`/v1/staff/queue`、
  `/v1/staff/submissions/{id}/dossier`
- `GET /public/catalog`、`GET /public/status?code=…`

投稿人凭证：finalize 返回 `contributor_token`（后续补传/换件/撤稿用）与
`public_code`（公开自查编号）。视频续传在 init 时可带 `media_meta`
（width/height/duration，生产环境由 ffprobe 适配器填入同构契约）。

## 可发布队列

处理完大文件续传、同图改裁剪、多人主张原作与渠道部分失败记录后：

```bash
$ python3 service.py ingest fixtures/legacy_manifest.json --data-dir ./data
...
可发布队列共 1 条
# 「河口日落长镜头」：活动页+报纸已成功，新媒体矩阵失败待重试；
#  存在权属争议的「铁桥与凌汛」被挡在人工核验；撤稿作品已停发。
```

队列只收**复核通过 + 人工确认原创 + 无未决争议/撤稿/停发 + 选用版本仍存在**
且至少一个渠道未成功的选用单。

## 数据与事件溯源

所有业务状态都可由 `data/events.sqlite` 的 `event` 表重放得到（事件只增不改），
原件永不原地覆盖。详见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)。

## 目录

```
riverflow/
  config.py       活动配置与冻结条款
  eventstore.py   SQLite 只增事件库、续传会话、幂等记录
  blobstore.py    内容哈希原件/转码件存储
  media.py        格式探针、PNG/BMP/PNM 解码、dHash64、视频侧车
  tech.py         可复核技术检查、缩略图/转码工作单
  submissions.py  续传、投稿事件、相似/搬运/合成信号（只转人工）
  editorial.py    人工裁决、权属争议、撤稿、版面、选用与复核
  distribution.py 渠道投递与幂等回执
  domain.py       事件目录、读模型投影、队列与公开目录
  app.py          门面与卷宗/脱敏视图
  api.py          HTTP API（标准库）
  ingest.py       存量记录导入
  demo.py         零依赖测试素材生成
tests/            62 个 pytest 用例
fixtures/         活动配置与存量记录清单
```
