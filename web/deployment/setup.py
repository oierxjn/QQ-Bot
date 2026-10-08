"""Initialize standalone source or Compose panel credentials and deployment identity."""

import argparse
import getpass
import json
import os
import re
import subprocess
from pathlib import Path

from .auth import create_credentials
from .config import ComposeDocument, DeploymentError, atomic_write, plain
from .toml_config import CONFIG_NAMES, ConfigStore


def initialize_source(root: Path, password=None):
    root = root.resolve()
    state = root / ".webcontroller"
    settings = {"root": str(root), "mode": "source"}
    settings_path = state / "settings.json"
    if settings_path.exists() and json.loads(settings_path.read_text(encoding="utf-8")) != settings:
        raise DeploymentError("已初始化的部署目录或模式不同，拒绝覆盖")
    store = ConfigStore(root, state)
    missing = []
    for name in CONFIG_NAMES:
        path = store.path(name)
        if not path.exists():
            template = path.with_suffix(path.suffix + ".template")
            if not template.is_file() or template.is_symlink():
                raise DeploymentError(f"缺少配置模板：{name}.template")
            missing.append((path, template.read_text(encoding="utf-8")))
    credentials = state / "credentials.json"
    if not credentials.exists():
        if password is None:
            password = getpass.getpass("设置面板管理员密码（至少 12 字符）: ")
            if password != getpass.getpass("再次输入密码: "):
                raise DeploymentError("两次密码不一致")
        atomic_write(credentials, json.dumps(create_credentials(password)))
    for path, source in missing:
        atomic_write(path, source)
    atomic_write(settings_path, json.dumps(settings))
    if os.name != "nt":
        state.chmod(0o700)
    return store


def discover_project(root: Path, explicit=None):
    result = subprocess.run(
        [
            "docker",
            "ps",
            "-a",
            "--format",
            '{{.Label "com.docker.compose.project"}}\t{{.Label "com.docker.compose.project.working_dir"}}',
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    projects = set()
    for line in result.stdout.splitlines():
        project, _, directory = line.partition("\t")
        if project and directory and Path(directory).resolve() == root.resolve():
            projects.add(project)
    if len(projects) > 1 and not explicit:
        raise DeploymentError("目录对应多个 Compose 项目，请使用 --project-name 明确指定")
    if explicit:
        if projects and explicit not in projects:
            raise DeploymentError("指定项目名与此目录已有容器不一致")
        project = explicit
    elif projects:
        project = next(iter(projects))
    else:
        name = plain(
            ComposeDocument.decode((root / "compose.yaml").read_text(encoding="utf-8"))
        ).get("name")
        env_path = root / ".env"
        if env_path.is_file():
            match = re.search(
                r"^\s*(?:export\s+)?COMPOSE_PROJECT_NAME\s*=\s*(.*?)\s*$",
                env_path.read_text(encoding="utf-8"),
                re.M,
            )
            if match:
                name = match.group(1).strip("\"'")
        if name and "$" in name:
            raise DeploymentError("Compose 项目名称包含变量，请使用 --project-name 明确指定")
        project = name or re.sub(r"[^a-z0-9_-]", "", root.name.lower()).lstrip("_-")
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", project):
        raise DeploymentError("无法确定合法的 Compose 项目名称，请使用 --project-name")
    return project


def initialize(root: Path, project: str, password=None):
    root = root.resolve()
    state = root / ".webcontroller"
    settings_path = state / "settings.json"
    settings = {"root": str(root), "project": project}
    if settings_path.exists() and json.loads(settings_path.read_text(encoding="utf-8")) != settings:
        raise DeploymentError("已初始化的部署目录或项目名不同，拒绝覆盖")
    document = ComposeDocument(root, state)
    document.parse(document.source())
    credentials = state / "credentials.json"
    if not credentials.exists():
        if password is None:
            password = getpass.getpass("设置面板管理员密码（至少 12 字符）: ")
            if password != getpass.getpass("再次输入密码: "):
                raise DeploymentError("两次密码不一致")
        atomic_write(credentials, json.dumps(create_credentials(password)))
    atomic_write(settings_path, json.dumps(settings))
    env_path = root / ".env"
    source = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
    keys = {
        "THERESA_DEPLOY_DIR": str(root),
        "THERESA_COMPOSE_PROJECT": project,
        "COMPOSE_PROJECT_NAME": project,
    }
    lines = [
        line
        for line in source.splitlines()
        if not re.match(
            r"\s*(?:export\s+)?(?:THERESA_DEPLOY_DIR|THERESA_COMPOSE_PROJECT|COMPOSE_PROJECT_NAME)\s*=",
            line,
        )
    ]
    for key, value in keys.items():
        escaped = value.replace("\\", "\\\\").replace("'", "\\'")
        lines.append(f"{key}='{escaped}'")
    atomic_write(env_path, "\n".join(lines) + "\n")
    for directory in (state, state / "backups", state / "tasks"):
        directory.mkdir(exist_ok=True, mode=0o700)
        directory.chmod(0o700)
    return document


def main():
    parser = argparse.ArgumentParser(description="初始化独立部署面板")
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--mode", choices=("source", "compose"), default="compose")
    parser.add_argument("--project-name")
    parser.add_argument("--reset-password", action="store_true")
    parser.add_argument("--accept-panel-upgrade", action="store_true")
    args = parser.parse_args()
    root = args.root.resolve()
    if args.mode == "source":
        if args.accept_panel_upgrade or args.project_name:
            parser.error("源码模式不接受 Compose 维护参数")
        if args.reset_password:
            settings = json.loads(
                (root / ".webcontroller/settings.json").read_text(encoding="utf-8")
            )
            if settings != {"root": str(root), "mode": "source"}:
                raise DeploymentError("部署目录或模式不一致")
            password = getpass.getpass("新管理员密码（至少 12 字符）: ")
            if password != getpass.getpass("再次输入密码: "):
                raise DeploymentError("两次密码不一致")
            atomic_write(
                root / ".webcontroller/credentials.json", json.dumps(create_credentials(password))
            )
        initialize_source(root)
        print("源码面板初始化完成，执行 uv run -m web.deployment --mode source --root .")
        return
    project = discover_project(root, args.project_name)
    # Explicit host-side maintenance commands; never exposed through the web API.
    state = root / ".webcontroller"
    settings_path = state / "settings.json"
    if settings_path.exists() and json.loads(settings_path.read_text(encoding="utf-8")) != {
        "root": str(root),
        "project": project,
    }:
        raise DeploymentError("已初始化的部署目录或项目名不同，拒绝执行维护操作")
    if args.reset_password:
        password = getpass.getpass("新管理员密码（至少 12 字符）: ")
        if password != getpass.getpass("再次输入密码: "):
            raise DeploymentError("两次密码不一致")
        atomic_write(state / "credentials.json", json.dumps(create_credentials(password)))
    if args.accept_panel_upgrade:
        protected = ComposeDocument.protection(
            ComposeDocument.decode((root / "compose.yaml").read_text(encoding="utf-8"))
        )
        atomic_write(state / "protected.json", json.dumps(protected, ensure_ascii=False))
    initialize(root, project)
    print(f"面板初始化完成，Compose 项目：{project}")
    print("执行 docker compose up -d webcontroller 后访问 http://127.0.0.1:7001")


if __name__ == "__main__":
    main()
