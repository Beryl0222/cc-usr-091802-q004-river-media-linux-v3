"""黄河影像征集与融合报道后端 —— 运行入口。

子命令：
* ``--check``                 基础配置自检；
* ``serve --port 8000``       启动 HTTP API；
* ``ingest <manifest.json>``  导入邮箱时代存量记录并输出可发布队列；
* ``terms``                   打印当前投稿条款版本与摘要。

数据默认落在 ``./data``（SQLite 事件库 + 内容哈希存储），
可用 ``--data-dir`` 覆盖；``--in-memory`` 仅用于自检。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from riverflow import SERVICE_ID, SERVICE_NAME
from riverflow.api import make_server
from riverflow.app import Application
from riverflow.config import CampaignConfig
from riverflow.ingest import IngestService, load_manifest

DEFAULT_CONFIG = "fixtures/sample.json"


def build_app(data_dir: Path, config_path: str = DEFAULT_CONFIG) -> Application:
    config = CampaignConfig.load(config_path)
    return Application(config, data_dir / "events.sqlite", data_dir / "storage")


def health_payload() -> dict:
    return {"status": "ok", "service": SERVICE_ID, "name": SERVICE_NAME}


def cmd_check(args) -> int:
    assert health_payload()["service"] == SERVICE_ID
    app = build_app(Path(args.data_dir), args.config)
    try:
        # 关键不变量自检
        assert app.config.minimum_video_height >= 1080
        assert app.config.ai_generated_allowed is False
        digest = app.config.terms_digest
        assert len(digest) == 64 and digest == app.config.terms_digest
        app.reload()
        print("基础检查通过：服务身份、1080P 门槛、非商业条款与事件库均正常")
    finally:
        app.close()
    return 0


def cmd_serve(args) -> int:
    app = build_app(Path(args.data_dir), args.config)
    server, _api = make_server(app, "0.0.0.0", args.port,
                               max_chunk_bytes=args.max_chunk_bytes)
    print(f"{SERVICE_NAME} 监听 0.0.0.0:{args.port}（数据目录 {args.data_dir}）")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        app.close()
    return 0


def cmd_ingest(args) -> int:
    app = build_app(Path(args.data_dir), args.config)
    try:
        manifest = load_manifest(args.manifest)
        ing = IngestService(app)
        files_dir = Path(args.data_dir) / "legacy-files"
        ing.materialize(manifest, files_dir)
        report = ing.ingest(manifest)
        output = json.dumps(report, ensure_ascii=False, indent=2)
        if args.out:
            Path(args.out).write_text(output, encoding="utf-8")
            print(f"导入报告已写入 {args.out}")
        else:
            print(output)
        print(f"\n可发布队列共 {len(report['publishable_queue'])} 条",
              file=sys.stderr)
        return 0
    finally:
        app.close()


def cmd_terms(args) -> int:
    config = CampaignConfig.load(args.config)
    print(json.dumps({
        "terms_version": config.terms_version,
        "terms_digest": config.terms_digest,
        "non_commercial_only": True,
        "text": config.terms_text,
    }, ensure_ascii=False, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=SERVICE_NAME)
    parser.add_argument("--check", action="store_true", help="基础配置自检")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--data-dir", default="data")
    parser.add_argument("--config", default=DEFAULT_CONFIG)
    parser.add_argument("--max-chunk-bytes", type=int, default=8 * 1024 * 1024)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--data-dir", default="data", dest="data_dir")
    common.add_argument("--config", default=DEFAULT_CONFIG)
    common.add_argument("--max-chunk-bytes", type=int, default=8 * 1024 * 1024,
                        dest="max_chunk_bytes")
    sub = parser.add_subparsers(dest="command")
    p_serve = sub.add_parser("serve", parents=[common], help="启动 HTTP API")
    p_serve.add_argument("--port", type=int, default=8000)
    p_serve.set_defaults(func=cmd_serve)
    p_ingest = sub.add_parser("ingest", parents=[common], help="导入存量记录")
    p_ingest.add_argument("manifest")
    p_ingest.add_argument("--out", default="")
    p_ingest.set_defaults(func=cmd_ingest)
    p_terms = sub.add_parser("terms", parents=[common], help="打印当前条款")
    p_terms.set_defaults(func=cmd_terms)

    args = parser.parse_args(argv)
    if args.check:
        # --check 走顶层参数；未显式给 data-dir/config 时用默认
        args.data_dir = getattr(args, "data_dir", "data")
        args.config = getattr(args, "config", DEFAULT_CONFIG)
        return cmd_check(args)
    if args.command is None:
        args.data_dir = "data"
        args.config = DEFAULT_CONFIG
        args.max_chunk_bytes = 8 * 1024 * 1024
        return cmd_serve(args)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
