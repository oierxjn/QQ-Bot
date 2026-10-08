import asyncio
import hmac
import json
import re
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .auth import Authentication
from .config import MAX_SOURCE, ComposeDocument, DeploymentError, atomic_write, plain
from .toml_config import CONFIG_NAMES, ConfigStore, form_values


class Draft(BaseModel):
    source: str = Field(max_length=MAX_SOURCE)
    version: str = Field(pattern=r"^[a-f0-9]{64}$")


class Version(BaseModel):
    version: str = Field(pattern=r"^[a-f0-9]{64}$")


class Patch(BaseModel):
    source: str = Field(max_length=MAX_SOURCE)
    service: str = Field(max_length=128)
    changes: dict


class Login(BaseModel):
    password: str = Field(max_length=1024)


class ConfigChange(BaseModel):
    path: list[str] = Field(min_length=1, max_length=3)
    value: str | bool | int | dict | None = None
    delete: bool = False


class ConfigPatch(BaseModel):
    source: str = Field(max_length=MAX_SOURCE)
    changes: list[ConfigChange] = Field(max_length=1000)


class TaskManager:
    def __init__(self, document, runner):
        self.document = document
        self.runner = runner
        self.lock = asyncio.Lock()
        self.worker = None
        self.directory = document.state / "tasks"
        self.directory.mkdir(exist_ok=True, mode=0o700)
        for path in self.directory.glob("*.json"):
            task = json.loads(path.read_text(encoding="utf-8"))
            if task["state"] == "running":
                task["state"] = "interrupted"
                task["output"] += (
                    "\n面板重启，任务执行结果不确定，请刷新服务状态后决定是否重新应用。"
                )
                self.persist(task)

    def persist(self, task):
        atomic_write(self.directory / (task["id"] + ".json"), json.dumps(task, ensure_ascii=False))

    def read(self, task_id):
        if not re.fullmatch(r"[a-f0-9]{32}", task_id):
            raise DeploymentError("任务不存在", 404)
        path = self.directory / (task_id + ".json")
        if not path.is_file():
            raise DeploymentError("任务不存在", 404)
        return json.loads(path.read_text(encoding="utf-8"))

    def latest(self):
        paths = sorted(
            self.directory.glob("*.json"), key=lambda path: path.stat().st_mtime, reverse=True
        )
        return self.read(paths[0].stem) if paths else None

    def available(self):
        if self.lock.locked():
            raise DeploymentError("已有保存、恢复或应用操作正在运行", 409)

    async def start(self, version):
        self.available()
        await self.lock.acquire()
        try:
            source = self.document.check_version(version)
            self.document.parse(source)
            await self.runner.validate(source)
            self.document.check_version(version)
            task = {
                "id": uuid.uuid4().hex,
                "state": "running",
                "version": version,
                "created_at": datetime.now(UTC).isoformat(),
                "output": "校验通过，准备应用。\n",
                "services": [],
            }
            self.persist(task)
            # Once application starts, the previous successful marker is no longer reliable.
            (self.document.state / "applied.json").unlink(missing_ok=True)
            self.worker = asyncio.create_task(self.run(task, source))
            return task
        except BaseException:
            self.lock.release()
            raise

    async def run(self, task, source):
        def emit(output):
            task["output"] = (task["output"] + output)[-128 * 1024 :]
            self.persist(task)

        try:
            task["services"] = await self.runner.apply(source, emit)
            self.document.check_version(task["version"])
            task["state"] = "succeeded"
            atomic_write(
                self.document.state / "applied.json", json.dumps({"version": task["version"]})
            )
            emit("\nCompose 命令完成；服务状态已刷新。\n")
        except asyncio.CancelledError:
            task["state"] = "interrupted"
            emit("\n面板停止，操作已中断，请检查实际容器状态。\n")
        except (DeploymentError, TimeoutError) as exc:
            task["state"] = "failed"
            emit("\n" + (str(exc) or "Docker 操作超时，请检查实际容器状态") + "\n")
        except Exception:
            task["state"] = "failed"
            emit("\n应用发生内部错误，请检查 Docker 和状态目录后重试。\n")
        finally:
            self.persist(task)
            self.lock.release()


def create_app(document: ComposeDocument | None, runner, credentials, *, configs=None):
    authentication = Authentication(credentials)
    login_lock = asyncio.Lock()
    tasks = TaskManager(document, runner) if document is not None else None
    if configs is None:
        if document is None:
            raise ValueError("源码模式需要配置目录")
        configs = ConfigStore(document.root, document.state)
    operation_lock = tasks.lock if tasks else asyncio.Lock()

    @asynccontextmanager
    async def lifespan(app):
        yield
        if tasks and tasks.worker and not tasks.worker.done():
            tasks.worker.cancel()
            await tasks.worker

    app = FastAPI(
        title="Theresa Deployment",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.tasks = tasks
    app.state.document = document
    app.state.runner = runner
    assets = Path(__file__).parent / "static"
    app.mount("/assets", StaticFiles(directory=assets), name="assets")

    @app.exception_handler(DeploymentError)
    async def deployment_error(request, exc):
        return JSONResponse({"error": {"message": str(exc)}}, status_code=exc.status)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request, exc):
        # Pydantic's default response includes the rejected input, potentially a password.
        return JSONResponse(
            {"error": {"message": "请求字段或格式不正确，请检查输入"}}, status_code=422
        )

    @app.middleware("http")
    async def secure(request: Request, call_next):
        try:
            if request.url.hostname not in {"localhost", "127.0.0.1", "::1"}:
                raise DeploymentError("面板仅支持 localhost 或回环地址访问", 400)
            length = request.headers.get("content-length")
            if length and (not length.isdigit() or int(length) > 2 * MAX_SOURCE):
                raise DeploymentError("请求过大", 413)
            if request.url.path.startswith("/api/"):
                if request.method not in {"GET", "HEAD"}:
                    body = bytearray()
                    async for chunk in request.stream():
                        body.extend(chunk)
                        if len(body) > 2 * MAX_SOURCE:
                            raise DeploymentError("请求过大", 413)
                    request._body = bytes(body)
                if request.method not in {"GET", "HEAD"}:
                    origin = request.headers.get("origin")
                    if origin and origin != str(request.base_url).rstrip("/"):
                        raise DeploymentError("请求来源不匹配", 403)
                if request.url.path != "/api/login":
                    session = authentication.authenticate(request.cookies.get("panel_session", ""))
                    if request.method not in {"GET", "HEAD"} and not hmac.compare_digest(
                        request.headers.get("x-csrf-token", ""), session["csrf"]
                    ):
                        raise DeploymentError("CSRF 校验失败，请重新登录", 403)
                    request.state.session = session
                if document is None and request.url.path.startswith(
                    ("/api/compose", "/api/backups", "/api/services", "/api/tasks")
                ):
                    raise DeploymentError("源码模式不提供 Compose 管理", 404)
            response = await call_next(request)
        except DeploymentError as exc:
            response = JSONResponse({"error": {"message": str(exc)}}, status_code=exc.status)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        )
        return response

    @app.get("/", response_class=HTMLResponse)
    async def index():
        return (Path(__file__).parent / "templates" / "deployment.html").read_text(encoding="utf-8")

    @app.post("/api/login")
    async def login(body: Login):
        async with login_lock:
            token, session = await asyncio.to_thread(authentication.login, body.password)
        response = JSONResponse({"csrf": session["csrf"]})
        response.set_cookie(
            "panel_session", token, httponly=True, samesite="strict", max_age=8 * 3600, path="/"
        )
        return response

    @app.get("/api/session")
    async def session(request: Request):
        return {"csrf": request.state.session["csrf"]}

    @app.post("/api/logout")
    async def logout(request: Request):
        authentication.logout(request.cookies.get("panel_session", ""))
        response = JSONResponse({"success": True})
        response.delete_cookie("panel_session", path="/")
        return response

    @app.get("/api/capabilities")
    async def capabilities():
        return {
            "mode": "compose" if document is not None else "source",
            "compose": document is not None,
            "configs": list(CONFIG_NAMES),
        }

    @app.get("/api/configs")
    async def config_list():
        return {
            "configs": [
                {"name": name, "exists": configs.path(name).is_file()} for name in CONFIG_NAMES
            ]
        }

    @app.get("/api/configs/{name}")
    async def read_config(name: str):
        return configs.read(name)

    @app.post("/api/configs/{name}/patch")
    async def patch_config(name: str, body: ConfigPatch):
        source = configs.patch(name, body.source, [change.model_dump() for change in body.changes])
        return {"source": source, "values": form_values(configs.validate(name, source).unwrap())}

    @app.post("/api/configs/{name}/validate")
    async def validate_config(name: str, body: Draft):
        configs.check_version(name, body.version)
        values = form_values(configs.validate(name, body.source).unwrap())
        return {"valid": True, "diff": configs.diff(name, body.source), "values": values}

    def config_available():
        if operation_lock.locked():
            raise DeploymentError("已有保存、恢复或应用操作正在运行", 409)

    @app.put("/api/configs/{name}")
    async def save_config(name: str, body: Draft):
        config_available()
        async with operation_lock:
            return configs.save(name, body.source, body.version)

    @app.get("/api/configs/{name}/backups")
    async def config_backups(name: str):
        return {"backups": configs.backups(name)}

    @app.get("/api/configs/{name}/backups/{backup_id}")
    async def preview_config_backup(name: str, backup_id: str):
        source = configs.backup_source(name, backup_id)
        return {"source": source, "diff": configs.diff(name, source)}

    @app.post("/api/configs/{name}/backups/{backup_id}/restore")
    async def restore_config(name: str, backup_id: str, body: Version):
        config_available()
        async with operation_lock:
            return configs.save(name, configs.backup_source(name, backup_id), body.version)

    @app.get("/api/compose")
    async def read_compose():
        result = document.read()
        applied = document.state / "applied.json"
        result["applied"] = (
            applied.exists()
            and json.loads(applied.read_text(encoding="utf-8")).get("version") == result["version"]
        )
        result["task"] = tasks.latest()
        return result

    @app.post("/api/compose/patch")
    async def patch_compose(body: Patch):
        source = document.patch(body.source, body.service, body.changes)
        return {"source": source, "services": plain(document.parse(source))["services"]}

    @app.post("/api/compose/parse")
    async def parse_compose(body: Draft):
        return {"services": plain(document.parse(body.source))["services"]}

    @app.post("/api/compose/validate")
    async def validate_compose(body: Draft):
        document.check_version(body.version)
        document.parse(body.source)
        await runner.validate(body.source)
        document.check_version(body.version)
        return {"valid": True, "diff": document.diff(body.source)}

    @app.put("/api/compose")
    async def save_compose(body: Draft):
        tasks.available()
        async with tasks.lock:
            document.check_version(body.version)
            document.parse(body.source)
            await runner.validate(body.source)
            result = document.save(body.source, body.version)
            result["applied"] = False
            return result

    @app.post("/api/compose/apply", status_code=202)
    async def apply_compose(body: Version):
        return await tasks.start(body.version)

    @app.get("/api/tasks/{task_id}")
    async def read_task(task_id: str):
        return tasks.read(task_id)

    @app.get("/api/backups")
    async def backups():
        return {"backups": document.backups()}

    @app.get("/api/backups/{backup_id}")
    async def preview_backup(backup_id: str):
        source = document.backup_source(backup_id)
        return {"source": source, "diff": document.diff(source)}

    @app.post("/api/backups/{backup_id}/restore")
    async def restore(backup_id: str, body: Version):
        tasks.available()
        async with tasks.lock:
            document.check_version(body.version)
            source = document.backup_source(backup_id)
            document.parse(source)
            await runner.validate(source)
            result = document.save(source, body.version)
            result["applied"] = False
            return result

    @app.get("/api/services")
    async def services():
        statuses = await runner.status()
        # Do not expose labels, commands, or raw inspect responses containing credentials.
        keys = {"Service", "Name", "State", "Health", "ExitCode"}
        return {
            "services": [
                {key: value for key, value in status.items() if key in keys} for status in statuses
            ]
        }

    return app
