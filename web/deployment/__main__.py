import argparse
import json
import os
from pathlib import Path

import uvicorn

from .app import create_app
from .config import ComposeDocument
from .runner import ComposeRunner
from .toml_config import ConfigStore


def build_app(root, mode, project=None):
    root = Path(root).resolve()
    state = root / ".webcontroller"
    settings = json.loads((state / "settings.json").read_text(encoding="utf-8"))
    if settings["root"] != str(root) or settings.get("mode", "compose") != mode:
        raise RuntimeError("部署目录/项目名与初始化记录不一致，请检查宿主机配置")
    if mode == "compose" and settings.get("project") != project:
        raise RuntimeError("Compose 项目名与初始化记录不一致")
    credentials = json.loads((state / "credentials.json").read_text(encoding="utf-8"))
    if mode == "source":
        return create_app(None, None, credentials, configs=ConfigStore(root, state))
    document = ComposeDocument(root, state)
    runner = ComposeRunner(root, settings["project"])
    return create_app(document, runner, credentials)


def main():
    parser = argparse.ArgumentParser(description="独立 WebController 配置面板")
    parser.add_argument("--mode", choices=("source", "compose"), default="compose")
    parser.add_argument("--root", type=Path)
    parser.add_argument("--port", type=int, default=7001)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("端口必须在 1 到 65535 之间")
    root = args.root or (
        Path.cwd() if args.mode == "source" else Path(os.environ["THERESA_DEPLOY_DIR"])
    )
    app = build_app(root, args.mode, os.environ.get("THERESA_COMPOSE_PROJECT"))
    uvicorn.run(
        app,
        host="127.0.0.1" if args.mode == "source" else "0.0.0.0",
        port=args.port,
        access_log=False,
        proxy_headers=False,
    )


if __name__ == "__main__":
    main()
