# 黄河影像征集流转

本项目承接黄河主题图片、视频和自述的征集、核验、编辑与多渠道刊发。作品原件、转码件、授权条款和发布回执分别保存标识与关联，联系方式不进入公开数据。

`fixtures/sample.json` 说明投稿格式、最低视频清晰度和可选发布渠道。原创性与内容合规需要人工确认，自动检查只提供技术结果和风险线索。

## 架构

- `riverflow/storage.py` — 内容寻址存储：原件按 sha256 只存一份，转码件挂在 `derivatives/<来源哈希>/<配方>/` 下。
- `riverflow/checks.py` — 技术检查（1080P、编码、时长等），报告记录规则版本、实测值与阈值，可复核、可复算。
- `riverflow/detection.py` — 相似 / 疑似搬运 / 疑似合成线索；只生成人工核验任务，检测分数不直接定性。
- `riverflow/core.py` — 领域服务：断点续传、版本与事件流、授权快照、选用 / 复核 / 发布职责分离、渠道回执幂等汇总、公开查询脱敏与逐件溯源。
- `riverflow/api.py` — JSON HTTP 接口薄层。

## 关键规则

- **断点续传**：按（投稿人, 内容哈希）幂等，重开会话不丢分片，重复完成不重复生成作品；完成时校验 sha256。
- **授权**：提交时记录条款版本与条款哈希；替换文件默认沿用原授权，重新征得授权会追加授权记录；发布不得超出授权渠道范围。
- **事件流**：补传说明、替换文件、权属争议（多人主张归入同一案件）、撤稿申请各自形成事件，全程可追溯。
- **撤稿**：停止后续分发（未发渠道取消），已入版面版本的刊发依据（条款版本、选用人、复核人、检查报告）保留。
- **职责分离**：编辑选用、复核员复核（与选用人不同）、发布员发布；公开查询只含已采用且未撤稿作品，不含联系方式。

## 运行

```bash
python3 service.py --check          # 基础配置检查
python3 service.py --port 8000      # 启动服务（/health 健康检查，/api/* 业务接口）
python3 -m unittest discover -s tests   # 场景测试
python3 -m unittest test_service        # 基线契约
```

主要接口：`POST /api/uploads` → `PUT /api/uploads/{id}/chunks` → `POST /api/uploads/{id}/complete` 完成投稿；`POST /api/works/{id}/select|approve|publish` 走编辑流程；`POST /api/receipts` 接收渠道回执（按回执号幂等）；`GET /api/queue/publishable` 可发布队列；`GET /api/works/{id}/provenance` 逐件溯源；`GET /api/public/works` 公开查询。
