import difflib
import hashlib
import io
import json
import os
import re
import tempfile
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

MAX_SOURCE = 1024 * 1024
FORM_FIELDS = {"image", "environment", "ports", "volumes", "restart", "depends_on", "healthcheck"}


class DeploymentError(Exception):
    def __init__(self, message: str, status: int = 422):
        super().__init__(message)
        self.status = status


def atomic_write(path: Path, source: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(prefix=".panel-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as output:
            output.write(source)
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def plain(value, depth=0, budget=None):
    if budget is None:
        budget = [100000]
    budget[0] -= 1
    if budget[0] < 0:
        raise DeploymentError("YAML 展开后的配置过大")
    if depth > 40:
        raise DeploymentError("YAML 嵌套过深或包含循环引用")
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise DeploymentError("YAML 配置键必须是字符串")
        return {key: plain(item, depth + 1, budget) for key, item in value.items()}
    if isinstance(value, list):
        return [plain(item, depth + 1, budget) for item in value]
    if value is None or isinstance(value, (str, bool, int, float)):
        # Reject custom tagged values: JSON/form editing cannot preserve their semantics.
        if getattr(value, "tag", None):
            raise DeploymentError("不支持自定义 YAML 标签")
        return value
    raise DeploymentError("不支持此 YAML 值类型")


def yaml_parser():
    parser = YAML()
    parser.preserve_quotes = True
    parser.allow_duplicate_keys = False
    parser.indent(mapping=4, sequence=4, offset=2)
    return parser


def merge_value(current, replacement):
    """Update round-trip containers in place so comments on surviving entries stay attached."""
    if isinstance(current, Mapping) and isinstance(replacement, dict):
        for key in list(current):
            if key not in replacement:
                del current[key]
        for key, value in replacement.items():
            current[key] = merge_value(current.get(key), value)
        return current
    if isinstance(current, list) and isinstance(replacement, list):
        for index, value in enumerate(replacement):
            if index < len(current):
                current[index] = merge_value(current[index], value)
            else:
                current.append(value)
        del current[len(replacement) :]
        return current
    return replacement


class ComposeDocument:
    def __init__(self, root: Path, state: Path, panel_service="webcontroller"):
        self.root = root.resolve()
        self.state = state.resolve()
        self.path = self.root / "compose.yaml"
        self.panel_service = panel_service
        self.state.mkdir(parents=True, exist_ok=True, mode=0o700)
        baseline = self.state / "protected.json"
        if baseline.exists():
            self.protected = json.loads(baseline.read_text(encoding="utf-8"))
        else:
            self.protected = self.protection(self.decode(self.source()), self.panel_service)
            atomic_write(baseline, json.dumps(self.protected, ensure_ascii=False))

    def source(self):
        if self.path.is_symlink():
            raise DeploymentError("compose.yaml 不能是符号链接")
        if self.path.stat().st_size > MAX_SOURCE:
            raise DeploymentError("Compose 文档超过 1 MiB")
        return self.path.read_text(encoding="utf-8")

    @staticmethod
    def version(source):
        return hashlib.sha256(source.encode("utf-8")).hexdigest()

    @staticmethod
    def decode(source):
        if len(source.encode("utf-8")) > MAX_SOURCE:
            raise DeploymentError("Compose 文档超过 1 MiB")
        try:
            document = yaml_parser().load(source)
            value = plain(document)
        except RecursionError:
            raise DeploymentError("YAML 嵌套过深") from None
        except YAMLError as exc:
            mark = getattr(exc, "problem_mark", None)
            location = f"（第 {mark.line + 1} 行，第 {mark.column + 1} 列）" if mark else ""
            raise DeploymentError(f"YAML 格式错误{location}") from None
        if not isinstance(value, dict) or not isinstance(value.get("services"), dict):
            raise DeploymentError("Compose 必须包含 services 映射")
        if "include" in value:
            raise DeploymentError("第一版只管理单一 compose.yaml，不支持 include")
        for key in ("volumes", "networks", "configs", "secrets"):
            if value.get(key) is not None and not isinstance(value[key], dict):
                raise DeploymentError(f"顶层 {key} 必须是映射")
        for name, service in value["services"].items():
            if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]*", name) or not isinstance(
                service, dict
            ):
                raise DeploymentError("服务名称或定义无效")
            if "extends" in service:
                raise DeploymentError("第一版不支持 extends；请将服务定义写入当前文档")
            field_types = {
                "image": str,
                "restart": str,
                "environment": (list, dict),
                "depends_on": (list, dict),
                "ports": list,
                "volumes": list,
                "volumes_from": list,
                "links": list,
                "healthcheck": dict,
                "networks": (list, dict),
            }
            for key, expected in field_types.items():
                if service.get(key) is not None and not isinstance(service[key], expected):
                    raise DeploymentError(f"{name} 的 {key} 字段类型不正确")
        return document

    @staticmethod
    def protection(document, panel_service="webcontroller"):
        value = plain(document)
        service = value["services"].get(panel_service)
        if not isinstance(service, dict):
            raise DeploymentError("缺少受保护的 webcontroller 服务")
        network_config = service.get("networks") or {"default": None}
        networks = list(network_config or {})
        volumes = []
        for mount in service.get("volumes") or []:
            if isinstance(mount, str):
                source = mount.split(":")[0]
                if source in value.get("volumes", {}):
                    volumes.append(source)
            elif isinstance(mount, dict) and mount.get("type") == "volume":
                volumes.append(mount.get("source"))
        return {
            "service": service,
            "name": value.get("name"),
            "networks": {name: value.get("networks", {}).get(name) for name in networks},
            "volumes": {name: value.get("volumes", {}).get(name) for name in volumes},
            "include": value.get("include"),
        }

    def parse(self, source):
        document = self.decode(source)
        if self.protection(document, self.panel_service) != self.protected:
            raise DeploymentError("不能修改面板服务、项目名称或面板使用的网络/卷；请在宿主机操作")
        for name, service in document["services"].items():
            if name == self.panel_service:
                continue
            if self.panel_service in (service.get("depends_on") or {}):
                raise DeploymentError("业务服务不能依赖面板服务，以免应用时重建面板")
            if any(
                service.get(key) == f"service:{self.panel_service}"
                for key in ("network_mode", "pid", "ipc")
            ):
                raise DeploymentError("业务服务不能共享面板服务的网络命名空间")
            if any(
                str(mount).split(":")[0] == self.panel_service
                for mount in service.get("volumes_from") or []
            ):
                raise DeploymentError("业务服务不能通过 volumes_from 依赖面板")
        return document

    def read(self):
        source = self.source()
        return {
            "source": source,
            "version": self.version(source),
            "services": plain(self.parse(source))["services"],
            "panel_service": self.panel_service,
        }

    def check_version(self, version):
        source = self.source()
        if self.version(source) != version:
            raise DeploymentError("配置已被其他操作修改，请重新读取并比较草稿", 409)
        return source

    def patch(self, source, service, changes):
        document = self.parse(source)
        if service == self.panel_service or service not in document["services"]:
            raise DeploymentError("此服务不可编辑")
        if set(changes) - FORM_FIELDS:
            raise DeploymentError("表单包含不支持的字段")
        for key, value in changes.items():
            if value is None:
                document["services"][service].pop(key, None)
            else:
                document["services"][service][key] = merge_value(
                    document["services"][service].get(key), value
                )
        output = io.StringIO()
        yaml_parser().dump(document, output)
        result = output.getvalue()
        self.parse(result)
        return result

    def diff(self, source):
        return "".join(
            difflib.unified_diff(
                self.source().splitlines(True),
                source.splitlines(True),
                fromfile="当前文件",
                tofile="草稿",
            )
        )

    def save(self, source, version):
        self.parse(source)
        current = self.check_version(version)
        if source != current:
            backup = datetime.now(UTC).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex + ".yaml"
            atomic_write(self.state / "backups" / backup, current)
            # Validation can take time; check again immediately before replacing the file.
            self.check_version(version)
            atomic_write(self.path, source)
        return self.read()

    def backups(self):
        directory = self.state / "backups"
        if not directory.exists():
            return []
        return [
            {"id": path.name, "size": path.stat().st_size}
            for path in sorted(directory.glob("*.yaml"), reverse=True)
            if not path.is_symlink()
        ]

    def backup_source(self, backup):
        if not re.fullmatch(r"\d{8}T\d{6}-[a-f0-9]{32}\.yaml", backup):
            raise DeploymentError("无效的备份标识", 404)
        path = self.state / "backups" / backup
        if not path.is_file() or path.is_symlink():
            raise DeploymentError("备份不存在", 404)
        return path.read_text(encoding="utf-8")
