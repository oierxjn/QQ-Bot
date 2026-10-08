import argparse
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import uvicorn

from . import default_source_root
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


def process_alive(pid):
    if os.name == "nt":
        import ctypes

        kernel32 = ctypes.windll.kernel32
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_bool, ctypes.c_uint32]
        handle = kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle(handle)
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def pid_record(root):
    return root / ".webcontroller" / "panel.pid"


def start_background(root, mode, port):
    state = root / ".webcontroller"
    if not state.is_dir():
        raise SystemExit("状态目录不存在，请先执行 setup 初始化")
    record = pid_record(root)
    if record.is_file():
        recorded = record.read_text(encoding="utf-8").strip()
        if recorded.isdigit() and process_alive(int(recorded)):
            raise SystemExit(f"面板已在后台运行（PID {recorded}），如需重启请先执行 --stop")
        record.unlink()
    log_path = state / "panel.log"
    command = [
        sys.executable,
        "-m",
        "web.deployment",
        "--mode",
        mode,
        "--root",
        str(root),
        "--port",
        str(port),
    ]
    flags = (
        {"creationflags": subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP}
        if os.name == "nt"
        else {"start_new_session": True}
    )
    with open(log_path, "ab") as log:
        child = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            **flags,
        )
    record.write_text(str(child.pid) + "\n", encoding="utf-8")
    time.sleep(1)
    if child.poll() is not None:
        record.unlink(missing_ok=True)
        raise SystemExit(f"后台面板启动失败，请查看日志：{log_path}")
    print(f"面板已在后台运行（PID {child.pid}）")
    print(f"日志：{log_path}")
    print(f"停止：uv run -m web.deployment --mode {mode} --stop")
    return child


def stop_background(root):
    record = pid_record(root)
    if not record.is_file():
        print("没有正在后台运行的面板")
        return
    pid = int(record.read_text(encoding="utf-8").strip())
    if not process_alive(pid):
        record.unlink()
        print(f"记录的 PID {pid} 已退出，清理残留记录")
        return
    if os.name == "nt":
        finished = subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True)
        if finished.returncode != 0:
            detail = (finished.stderr or finished.stdout).decode(errors="replace").strip()
            raise SystemExit(f"停止失败：{detail}")
    else:
        os.kill(pid, signal.SIGTERM)
    record.unlink(missing_ok=True)
    print(f"已停止后台面板（PID {pid}）")


def main():
    parser = argparse.ArgumentParser(description="独立 WebController 配置面板")
    parser.add_argument("--mode", choices=("source", "compose"), default="compose")
    parser.add_argument("--root", type=Path)
    parser.add_argument("--port", type=int, default=7001)
    parser.add_argument("--bg", action="store_true", help="以后台进程运行，日志写入状态目录")
    parser.add_argument("--stop", action="store_true", help="停止后台运行的面板")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("端口必须在 1 到 65535 之间")
    root = Path(
        args.root
        or (
            default_source_root()
            if args.mode == "source"
            else Path(os.environ["THERESA_DEPLOY_DIR"])
        )
    ).resolve()
    if args.stop:
        stop_background(root)
        return
    if args.bg:
        start_background(root, args.mode, args.port)
        return
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
