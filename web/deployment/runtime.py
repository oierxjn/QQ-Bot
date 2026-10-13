"""Panel runtime assembly: build the app, auto-initialize, and serve in-process.

The CLI entry and the Bot process share this module; the Bot starts it as a task
when THERESA_WEBCONTROLLER is enabled.
"""

import json
from pathlib import Path

import uvicorn

from . import default_source_root
from .app import create_app
from .config import ComposeDocument
from .runner import ComposeRunner
from .setup import (
    adopt_mode,
    discover_project,
    initialize,
    initialize_source,
    print_initial_password,
)
from .toml_config import ConfigStore

PANEL_DEPLOY_MOUNT = "/deploy"


def detect_runtime():
    """A /deploy bind mount means the Compose deployment; otherwise run on the checkout."""
    deploy = Path(PANEL_DEPLOY_MOUNT)
    if deploy.is_dir() and (deploy / "compose.yaml").is_file():
        return deploy.resolve(), "compose"
    return default_source_root(), "source"


def build_app(root, mode):
    root = Path(root).resolve()
    state = root / ".webcontroller"
    settings = json.loads((state / "settings.json").read_text(encoding="utf-8"))
    if settings.get("root") != str(root):
        raise RuntimeError(
            f"部署目录与初始化记录不一致：状态记录 {settings.get('root')!r}，当前为 {str(root)!r}"
        )
    if settings.get("mode", "compose") != mode:
        raise RuntimeError(
            f"初始化模式 {settings.get('mode', 'compose')!r} 与启动模式 {mode!r} 不一致"
        )
    credentials = json.loads((state / "credentials.json").read_text(encoding="utf-8"))
    if mode == "source":
        return create_app(None, None, credentials, configs=ConfigStore(root, state))
    document = ComposeDocument(root, state)
    runner = ComposeRunner(root, settings["project"])
    return create_app(document, runner, credentials)


def prepare(root, mode):
    """Auto-initialize on first run or adopt the recorded mode, then build the app."""
    root = Path(root).resolve()
    if not (root / ".webcontroller" / "settings.json").is_file():
        if mode == "source":
            initialize_source(root, None)
        else:
            initialize(root, discover_project(root), None)
        print_initial_password(root)
    else:
        adopt_mode(root, mode)
    return build_app(root, mode)


async def serve(port=7001, root=None, mode=None):
    """Detect the runtime context and serve the panel until the task is cancelled."""
    if root is None:
        root, mode = detect_runtime()
    app = prepare(root, mode)
    config = uvicorn.Config(
        app,
        host="127.0.0.1" if mode == "source" else "0.0.0.0",
        port=port,
        access_log=False,
        proxy_headers=False,
    )
    await uvicorn.Server(config).serve()
