"""Panel runtime assembly: build the app, auto-initialize, and serve in-process.

The CLI entry and the container share this module. In the container the panel is
the main process: it serves the web app and, when THERESA_BOT=1, supervises the
Bot startup chain as a child process with restart backoff. The panel and the Bot
are decoupled in both directions - a panel failure never stops the Bot, and a
Bot crash never stops the panel.
"""

import asyncio
import json
import os
import signal
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


def _notify(message):
    print(message, flush=True)


async def serve_panel(root, mode, port, stop, strict):
    """Serve the HTTP panel; `stop` is set only on a clean uvicorn exit (shutdown)."""
    try:
        app = prepare(root, mode)
        config = uvicorn.Config(
            app,
            host="127.0.0.1" if mode == "source" else "0.0.0.0",
            port=port,
            access_log=False,
            proxy_headers=False,
        )
        await uvicorn.Server(config).serve()
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        _notify(f"WebController 面板异常退出，Bot 不受影响：{exc}")
        if strict:
            raise
        return
    stop.set()


async def supervise_bot(stop, command, cwd, backoff_start=1.0, backoff_cap=30.0):
    """Run the Bot as a child process and restart it with backoff until `stop`."""
    backoff = backoff_start
    while not stop.is_set():
        try:
            process = await asyncio.create_subprocess_exec(*command, cwd=cwd)
        except OSError as exc:
            _notify(f"无法启动 Bot 进程，放弃拉起：{exc}")
            return
        _notify(f"Bot 进程已启动（PID {process.pid}）")
        waiter = asyncio.create_task(process.wait())
        stopper = asyncio.create_task(stop.wait())
        await asyncio.wait({waiter, stopper}, return_when=asyncio.FIRST_COMPLETED)
        stopper.cancel()
        if stop.is_set() and process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=5)
            except TimeoutError:
                process.kill()
                await process.wait()
        code = await waiter
        if stop.is_set():
            return
        _notify(f"Bot 进程退出（exit {code}），{backoff:g} 秒后重启")
        try:
            await asyncio.wait_for(stop.wait(), timeout=backoff)
        except TimeoutError:
            pass
        backoff = min(backoff * 2, backoff_cap)


async def serve(port=7001, root=None, mode=None):
    """Serve entry for the CLI and the container command.

    compose 模式下 THERESA_BOT=1 开启 Bot 子进程监督（容器主进程形态）；
    THERESA_WEBCONTROLLER=0 关闭面板 HTTP 服务（仅拉起 Bot 的形态）。
    """
    if root is None:
        root, mode = detect_runtime()
    root = Path(root).resolve()
    bot_enabled = mode == "compose" and os.environ.get("THERESA_BOT") == "1"
    panel_enabled = os.environ.get("THERESA_WEBCONTROLLER", "1") != "0"
    if not panel_enabled and not bot_enabled:
        raise SystemExit("THERESA_WEBCONTROLLER 与 THERESA_BOT 均未启用，没有可运行的服务")
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            # Covers the phases before/after uvicorn's own signal capture; as PID 1
            # the process would otherwise ignore SIGTERM entirely.
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):
            pass
    panel = bot = None
    if panel_enabled:
        panel = asyncio.create_task(serve_panel(root, mode, port, stop, strict=not bot_enabled))
    if bot_enabled:
        command = ["bash", "scripts/entrypoint.sh", "python3", "main.py"]
        bot = asyncio.create_task(supervise_bot(stop, command, default_source_root()))
    try:
        if panel is not None:
            await panel
        if stop.is_set() or bot is None:
            return
        # 面板先于停机请求结束（启动失败或崩溃）：Bot 继续运行，直到停机信号。
        await bot
    finally:
        stop.set()
        for task in (panel, bot):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(
            *(task for task in (panel, bot) if task is not None), return_exceptions=True
        )
