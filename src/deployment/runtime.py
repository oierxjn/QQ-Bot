"""Panel runtime assembly: build the app, auto-initialize, and serve in-process.

The panel is always the process that owns the Bot; the deployment shape only
changes the details. In the container the panel is the main process serving on
0.0.0.0 and supervising the entrypoint chain; in a source checkout it listens on
the loopback interface and supervises ``main.py`` directly. The panel and the
Bot are decoupled in both directions - a panel failure never stops the Bot, and
a Bot crash never stops the panel.
"""

import asyncio
import json
import signal
import sys
from pathlib import Path

import uvicorn

from . import default_source_root
from .app import create_app
from .config import ComposeDocument
from .runner import ComposeRunner
from .setup import (
    discover_project,
    initialize,
    initialize_source,
    print_initial_password,
)
from .toml_config import ConfigStore

PANEL_DEPLOY_MOUNT = "/deploy"


def detect_runtime(root=None):
    """A /deploy bind mount means the Compose deployment; otherwise run on the checkout.

    The mount also fixes the deploy root, so ``root`` only applies to source runs.
    """
    deploy = Path(PANEL_DEPLOY_MOUNT)
    if deploy.is_dir() and (deploy / "compose.yaml").is_file():
        return deploy.resolve(), "compose"
    return Path(root or default_source_root()).resolve(), "source"


def build_app(root):
    root = Path(root).resolve()
    _, mode = detect_runtime(root)
    state = root / ".webcontroller"
    settings = json.loads((state / "settings.json").read_text(encoding="utf-8"))
    if settings.get("root") != str(root):
        raise RuntimeError(
            f"部署目录与初始化记录不一致：状态记录 {settings.get('root')!r}，当前为 {str(root)!r}"
        )
    credentials = json.loads((state / "credentials.json").read_text(encoding="utf-8"))
    if mode == "source":
        return create_app(None, None, credentials, configs=ConfigStore(root, state))
    document = ComposeDocument(root, state)
    runner = ComposeRunner(root, settings["project"])
    return create_app(document, runner, credentials)


def prepare(root, mode):
    """Auto-initialize on first run, then build the app."""
    root = Path(root).resolve()
    if not (root / ".webcontroller" / "settings.json").is_file():
        if mode == "source":
            initialize_source(root, None)
        else:
            initialize(root, discover_project(root), None)
        print_initial_password(root)
    return build_app(root)


def _notify(message):
    print(message, flush=True)


async def serve_panel(root, mode, port, stop):
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
        kind = type(exc).__name__
        _notify(f"WebController 面板异常退出，Bot 不受影响（{kind}）：{exc}")
        return
    stop.set()


async def supervise_bot(stop, command, cwd, backoff_start=1.0, backoff_cap=600.0, reset_after=60.0):
    """Run the Bot as a child process and restart it with exponential backoff.

    A child that stayed up for `reset_after` seconds counts as stable and the
    next crash restarts at `backoff_start` again.
    """
    backoff = backoff_start
    while not stop.is_set():
        started = asyncio.get_running_loop().time()
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
        if asyncio.get_running_loop().time() - started >= reset_after:
            backoff = backoff_start
        _notify(f"Bot 进程退出（exit {code}），{backoff:g} 秒后重启")
        try:
            await asyncio.wait_for(stop.wait(), timeout=backoff)
        except TimeoutError:
            pass
        backoff = min(backoff * 2, backoff_cap)


async def serve(port=7001, root=None, mode=None):
    """Serve entry for the CLI and the container command.

    面板始终拉起并守护 Bot：容器里经 entrypoint 链（数据库初始化、QQ 登录等待）
    跑镜像侧代码，源码检出里直接跑 main.py。
    """
    if root is None or mode is None:
        root, mode = detect_runtime(root)
    root = Path(root).resolve()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            # Covers the phases before/after uvicorn's own signal capture; as PID 1
            # the process would otherwise ignore SIGTERM entirely.
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):
            pass
    if mode == "compose":
        # 容器内跑镜像侧代码，入口链含数据库初始化与 QQ 登录等待。
        command = ["bash", "scripts/entrypoint.sh", "python3", "main.py"]
        cwd = default_source_root()
    else:
        command = [sys.executable, "main.py"]
        cwd = root
    panel = asyncio.create_task(serve_panel(root, mode, port, stop))
    bot = asyncio.create_task(supervise_bot(stop, command, cwd))
    try:
        await panel
        if stop.is_set():
            return
        # 面板先于停机请求结束（启动失败或崩溃）：Bot 继续运行，直到停机信号。
        await bot
    finally:
        stop.set()
        for task in (panel, bot):
            if not task.done():
                task.cancel()
        await asyncio.gather(panel, bot, return_exceptions=True)
