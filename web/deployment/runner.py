import asyncio
import json
import os
import re
import tempfile
from pathlib import Path

from .config import ComposeDocument, DeploymentError, plain


def sensitive_values(value, values, sensitive=False):
    if isinstance(value, dict):
        for key, item in value.items():
            sensitive_values(
                item,
                values,
                sensitive
                or key == "environment"
                or bool(re.search(r"password|passwd|token|secret|api_key", key, re.I)),
            )
    elif isinstance(value, list):
        for item in value:
            if isinstance(item, str) and "=" in item:
                sensitive_values(item.split("=", 1)[1], values, sensitive)
            else:
                sensitive_values(item, values, sensitive)
    elif sensitive and value is not None and str(value):
        values.add(str(value))


class ComposeRunner:
    """Docker subprocess boundary. No request can supply arbitrary CLI arguments."""

    def __init__(
        self, root: Path, project: str, panel_service="webcontroller", verify_container=True
    ):
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", project):
            raise DeploymentError("无效的 Compose 项目名称")
        self.root = root.resolve()
        self.project = project
        self.panel_service = panel_service
        self.verify_container = verify_container
        self.prefix = [
            "docker",
            "compose",
            "--ansi",
            "never",
            "--project-directory",
            str(self.root),
            "-p",
            project,
        ]
        self.environment = {
            key: value for key, value in os.environ.items() if not key.startswith("COMPOSE_")
        }
        # v1 applies only default-profile services, irrespective of the panel's own environment.
        self.environment["COMPOSE_PROFILES"] = ""
        self.known_sensitive = set()

    def redaction_values(self, source):
        values = self.known_sensitive.copy()
        if source:
            sensitive_values(plain(ComposeDocument.decode(source)), values)
        env_file = self.root / ".env"
        if env_file.is_file():
            for line in env_file.read_text(encoding="utf-8").splitlines():
                if "=" in line and not line.lstrip().startswith("#"):
                    value = line.split("=", 1)[1].strip().strip("\"'")
                    if value:
                        values.add(value)
        return values

    def redact(self, text, source="", values=None):
        if values is None:
            values = self.redaction_values(source)
        for value in sorted(values, key=len, reverse=True):
            text = text.replace(value, "[REDACTED]")
        text = re.sub(r"(://[^\s:/]+:)[^\s@]+@", r"\1[REDACTED]@", text)
        return re.sub(
            r"(?i)((?:password|passwd|token|secret|api_key)\s*[=:]\s*)[^\s,]+",
            r"\1[REDACTED]",
            text,
        )

    async def execute(self, args, source="", emit=None, timeout=60):
        # Parse YAML once per command, rather than on every streamed output line.
        values = self.redaction_values(source)
        try:
            process = await asyncio.create_subprocess_exec(
                *args,
                cwd=self.root,
                env=self.environment,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError:
            raise DeploymentError(
                "无法启动 Docker CLI，请检查面板镜像和 Docker socket", 503
            ) from None
        chunks = {"stdout": bytearray(), "stderr": bytearray()}

        async def read(stream, name):
            pending = bytearray()
            while chunk := await stream.read(4096):
                chunks[name].extend(chunk)
                if len(chunks[name]) > 4 * 1024 * 1024:
                    raise DeploymentError("Docker 输出超过限制", 503)
                if emit:
                    pending.extend(chunk)
                    while b"\n" in pending:
                        line, _, remainder = pending.partition(b"\n")
                        pending = bytearray(remainder)
                        emit(self.redact(line.decode(errors="replace") + "\n", values=values))
                    if len(pending) > 64 * 1024:
                        raise DeploymentError("Docker 单行输出超过限制", 503)
            if emit and pending:
                emit(self.redact(pending.decode(errors="replace"), values=values))

        async def monitor():
            await asyncio.gather(
                read(process.stdout, "stdout"), read(process.stderr, "stderr"), process.wait()
            )

        try:
            await asyncio.wait_for(monitor(), timeout)
        except (TimeoutError, asyncio.CancelledError, DeploymentError) as exc:
            if process.returncode is None:
                process.kill()
            await process.wait()
            if isinstance(exc, TimeoutError):
                raise DeploymentError("Docker 操作超时，请检查实际容器状态", 503) from None
            raise
        stdout = chunks["stdout"].decode(errors="replace")
        stderr = chunks["stderr"].decode(errors="replace")
        if process.returncode:
            raise DeploymentError(
                "Docker 操作失败：" + self.redact(stderr or stdout, values=values)[-8000:], 503
            )
        return stdout

    async def identity(self):
        if not self.verify_container:
            return
        hostname = os.environ.get("HOSTNAME", "")
        if not re.fullmatch(r"[a-f0-9]{12,64}", hostname):
            raise DeploymentError("面板必须在默认主机名的独立容器内运行", 503)
        result = json.loads(await self.execute(["docker", "inspect", hostname]))[0]
        labels = result.get("Config", {}).get("Labels", {})
        if (
            labels.get("com.docker.compose.project") != self.project
            or labels.get("com.docker.compose.service") != self.panel_service
        ):
            raise DeploymentError("面板容器的 Compose 项目身份与配置不一致", 503)
        if not any(
            mount.get("Type") == "bind"
            and mount.get("Source") == str(self.root)
            and mount.get("Destination") == str(self.root)
            and mount.get("RW")
            for mount in result.get("Mounts", [])
        ):
            raise DeploymentError("部署目录必须以相同绝对路径读写挂载进面板", 503)

    async def validate(self, source):
        await self.identity()
        descriptor, temporary = tempfile.mkstemp(
            prefix=".compose-check-", suffix=".yaml", dir=self.root
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                output.write(source)
            args = self.prefix + ["-f", temporary]
            await self.execute(args + ["config", "--quiet"], source)
            result = json.loads(await self.execute(args + ["config", "--format", "json"], source))
            sensitive_values(result, self.known_sensitive)
            services = result.get("services", {})
            for name, service in services.items():
                if name == self.panel_service:
                    continue
                references = set(service.get("depends_on", {}))
                references.update(
                    str(item).split(":")[0] for item in service.get("volumes_from", [])
                )
                references.update(str(item).split(":")[0] for item in service.get("links", []))
                if self.panel_service in references or any(
                    service.get(key) == f"service:{self.panel_service}"
                    for key in ("network_mode", "pid", "ipc")
                ):
                    raise DeploymentError("业务服务不能依赖面板服务")
                for mount in service.get("volumes", []):
                    if mount.get("type") == "bind":
                        host_path = Path(mount["source"]).resolve()
                        # config --quiet cannot verify host resources; relative binds must exist
                        # in the shared directory to avoid silently creating empty directories.
                        if host_path.is_relative_to(self.root) and not host_path.exists():
                            raise DeploymentError(f"挂载源路径不存在：{host_path}")
            return result
        finally:
            Path(temporary).unlink(missing_ok=True)

    async def status(self):
        await self.identity()
        output = await self.execute(
            self.prefix + ["-f", str(self.root / "compose.yaml"), "ps", "--all", "--format", "json"]
        )
        try:
            value = json.loads(output or "[]")
            statuses = value if isinstance(value, list) else [value]
        except json.JSONDecodeError:
            statuses = [json.loads(line) for line in output.splitlines() if line.strip()]
        keys = {"Service", "Name", "State", "Health", "ExitCode"}
        return [{key: value for key, value in status.items() if key in keys} for status in statuses]

    async def apply(self, source, emit):
        config = await self.validate(source)
        targets = [
            name
            for name, service in config["services"].items()
            if name != self.panel_service and not service.get("profiles")
        ]
        if not targets:
            raise DeploymentError("没有可应用的默认 profile 服务")
        if (self.root / "compose.yaml").read_text(encoding="utf-8") != source:
            raise DeploymentError("配置已被其他操作修改", 409)
        emit("开始应用：" + ", ".join(targets) + "\n")
        await self.execute(
            self.prefix
            + [
                "-f",
                str(self.root / "compose.yaml"),
                "up",
                "-d",
                "--wait",
                "--wait-timeout",
                "120",
                "--",
            ]
            + targets,
            source,
            emit,
            timeout=900,
        )
        return await self.status()
