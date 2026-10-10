"""Initialize standalone source or Compose panel credentials and deployment identity."""

import json
import os
import re
import subprocess
from pathlib import Path

from .auth import create_credentials, generate_password
from .config import ComposeDocument, DeploymentError, atomic_write, plain
from .toml_config import CONFIG_NAMES, ConfigStore


def initialize_source(root: Path, password=None):
    root = root.resolve()
    state = root / ".webcontroller"
    settings = {"root": str(root), "mode": "source"}
    settings_path = state / "settings.json"
    if settings_path.exists():
        existing = json.loads(settings_path.read_text(encoding="utf-8"))
        if existing.get("root") != settings["root"]:
            raise DeploymentError(
                f"已初始化的部署目录不同：状态记录 {existing.get('root')!r}，当前 {settings['root']!r}"
            )
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
            password = generate_password()
            atomic_write(state / "initial-password", password + "\n")
        atomic_write(credentials, json.dumps(create_credentials(password)))
    for path, source in missing:
        atomic_write(path, source)
    atomic_write(settings_path, json.dumps(settings))
    if os.name != "nt":
        state.chmod(0o700)
    return store


def discover_project(root: Path, explicit=None, local=None):
    base = (local or root).resolve()
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
            ComposeDocument.decode((base / "compose.yaml").read_text(encoding="utf-8"))
        ).get("name")
        env_path = base / ".env"
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


def initialize(root: Path, project: str, password=None, local=None):
    root = root.resolve()
    local = (local or root).resolve()
    state = local / ".webcontroller"
    settings_path = state / "settings.json"
    settings = {"root": str(root), "project": project, "mode": "compose"}
    if settings_path.exists():
        existing = json.loads(settings_path.read_text(encoding="utf-8"))
        if existing.get("root") != str(root):
            raise DeploymentError(
                f"已初始化的部署目录不同：状态记录 {existing.get('root')!r}，当前 {str(root)!r}"
            )
        if existing.get("project", project) != project:
            raise DeploymentError(
                f"已初始化的 Compose 项目名不同：状态记录 {existing.get('project')!r}，当前 {project!r}"
            )
    document = ComposeDocument(local, state)
    document.parse(document.source())
    credentials = state / "credentials.json"
    if not credentials.exists():
        if password is None:
            password = generate_password()
            atomic_write(state / "initial-password", password + "\n")
        atomic_write(credentials, json.dumps(create_credentials(password)))
    atomic_write(settings_path, json.dumps(settings))
    for directory in (state, state / "backups", state / "tasks"):
        directory.mkdir(exist_ok=True, mode=0o700)
        directory.chmod(0o700)
    return document


def adopt_mode(root: Path, mode: str, local=None):
    """Adopt the startup mode when the deploy root matches, keeping credentials and backups."""
    root = root.resolve()
    local = (local or root).resolve()
    path = local / ".webcontroller" / "settings.json"
    settings = json.loads(path.read_text(encoding="utf-8"))
    if settings.get("root") != str(root):
        raise DeploymentError(
            f"状态目录记录的部署目录为 {settings.get('root')!r}，与当前 {str(root)!r} 不同"
        )
    if settings.get("mode", "compose") == mode:
        return
    if mode == "compose":
        settings["project"] = discover_project(root, None, local=local)
        settings["mode"] = "compose"
    else:
        settings.pop("project", None)
        settings["mode"] = "source"
    atomic_write(path, json.dumps(settings))


def print_initial_password(local: Path):
    initial = local / ".webcontroller" / "initial-password"
    if initial.exists():
        print(f"初始管理员密码已生成：{initial}")
        print("首次成功登录后面板会删除该文件；请尽快用 --reset-password 修改密码。")
