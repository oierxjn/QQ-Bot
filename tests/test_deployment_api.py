import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from tests.test_deployment_config import SOURCE
from web.deployment.app import TaskManager, create_app
from web.deployment.auth import create_credentials
from web.deployment.config import ComposeDocument, DeploymentError
from web.deployment.runner import ComposeRunner


class FakeRunner:
    def __init__(self):
        self.calls = []
        self.failure = False

    async def validate(self, source):
        self.calls.append("validate")
        if self.failure:
            raise DeploymentError("Compose 校验失败")
        return {"services": {"db": {"image": "postgres:18"}, "webcontroller": {"image": "panel:1"}}}

    async def apply(self, source, emit):
        self.calls.append("apply")
        emit("db recreated")
        await asyncio.sleep(0.02)
        return [{"Service": "db", "State": "running", "Health": "healthy"}]

    async def status(self):
        return []


@pytest.fixture
def panel(tmp_path):
    (tmp_path / "compose.yaml").write_text(SOURCE, encoding="utf-8")
    state = tmp_path / ".webcontroller"
    document = ComposeDocument(tmp_path, state)
    runner = FakeRunner()
    credentials = create_credentials("test-password")
    with TestClient(
        create_app(document, runner, credentials), base_url="http://localhost"
    ) as client:
        yield client, document, runner


def login(client):
    response = client.post("/api/login", json={"password": "test-password"})
    assert response.status_code == 200
    return {"X-CSRF-Token": response.json()["csrf"]}


def test_authentication_and_csrf_required(panel):
    client, document, runner = panel
    assert client.get("/api/compose").status_code == 401
    assert client.post("/api/login", json={"password": "wrong"}).status_code == 401
    headers = login(client)
    assert client.get("/api/compose").status_code == 200
    body = {"source": SOURCE, "version": document.read()["version"]}
    assert client.put("/api/compose", json=body).status_code == 403
    assert (
        client.put(
            "/api/compose", json=body, headers={**headers, "Origin": "http://evil.test"}
        ).status_code
        == 403
    )
    assert runner.calls == []


def test_validation_failure_does_not_write_file(panel):
    client, document, runner = panel
    runner.failure = True
    body = {
        "source": SOURCE.replace("postgres:18", "postgres:19"),
        "version": document.read()["version"],
    }
    response = client.put("/api/compose", json=body, headers=login(client))
    assert response.status_code == 422
    assert document.source() == SOURCE
    assert document.backups() == []


def test_save_apply_poll_and_logout(panel):
    client, document, runner = panel
    headers = login(client)
    result = client.put(
        "/api/compose",
        json={
            "source": SOURCE.replace("postgres:18", "postgres:19"),
            "version": document.read()["version"],
        },
        headers=headers,
    )
    assert result.status_code == 200
    result = client.post(
        "/api/compose/apply", json={"version": result.json()["version"]}, headers=headers
    )
    assert result.status_code == 202
    task_id = result.json()["id"]
    import time

    for _ in range(50):
        task = client.get(f"/api/tasks/{task_id}").json()
        if task["state"] != "running":
            break
        time.sleep(0.01)
    assert task["state"] == "succeeded"
    assert "db recreated" in task["output"]
    assert client.get("/api/compose").json()["applied"] is True
    assert client.post("/api/logout", headers=headers).status_code == 200
    assert client.get("/api/compose").status_code == 401


def test_limiter_blocks_repeated_failed_logins(panel):
    client, _, _ = panel
    for _ in range(5):
        assert client.post("/api/login", json={"password": "wrong"}).status_code == 401
    assert client.post("/api/login", json={"password": "wrong"}).status_code == 429


def test_restart_marks_unfinished_task_interrupted(panel):
    _, document, runner = panel
    path = document.state / "tasks" / ("a" * 32 + ".json")
    path.parent.mkdir(exist_ok=True)
    path.write_text(
        json.dumps({"id": "a" * 32, "state": "running", "output": "", "version": "old"}),
        encoding="utf-8",
    )
    with TestClient(
        create_app(document, runner, create_credentials("test-password")),
        base_url="http://localhost",
    ) as client:
        login(client)
        assert client.get("/api/tasks/" + "a" * 32).json()["state"] == "interrupted"


@pytest.mark.asyncio
async def test_runner_uses_fixed_project_context_and_redacts_output(tmp_path):
    (tmp_path / "compose.yaml").write_text(SOURCE, encoding="utf-8")
    (tmp_path / ".env").write_text("DB_PASSWORD=private-value\n", encoding="utf-8")
    runner = ComposeRunner(tmp_path, "original-project", verify_container=False)
    commands = []

    async def execute(args, source="", emit=None, timeout=60):
        commands.append(args)
        if "config" in args:
            return json.dumps(
                {
                    "services": {
                        "webcontroller": {},
                        "db": {},
                        "optional": {"profiles": ["optional"]},
                    }
                }
            )
        if "ps" in args:
            return "[]"
        if emit:
            emit(runner.redact("private-value secret", source))
        return ""

    runner.execute = execute
    output = []
    await runner.apply(SOURCE, output.append)
    up = next(args for args in commands if "up" in args)
    assert up[-1] == "db"
    assert "optional" not in up
    assert "webcontroller" not in up
    assert "down" not in up and "--remove-orphans" not in up
    assert "original-project" in up
    assert str(tmp_path / "compose.yaml") in up
    assert "private-value" not in "".join(output)
    assert "secret" not in "".join(output)


@pytest.mark.asyncio
async def test_running_apply_blocks_save_and_second_apply(tmp_path):
    (tmp_path / "compose.yaml").write_text(SOURCE, encoding="utf-8")
    document = ComposeDocument(tmp_path, tmp_path / ".webcontroller")
    runner = FakeRunner()
    started = asyncio.Event()
    release = asyncio.Event()

    async def apply(source, emit):
        started.set()
        await release.wait()
        return []

    runner.apply = apply
    tasks = TaskManager(document, runner)
    task = await tasks.start(document.read()["version"])
    await started.wait()
    with pytest.raises(DeploymentError, match="正在运行"):
        tasks.available()
    with pytest.raises(DeploymentError):
        await tasks.start(document.read()["version"])
    release.set()
    await tasks.worker
    assert tasks.read(task["id"])["state"] == "succeeded"
    tasks.available()


@pytest.mark.asyncio
async def test_failed_apply_is_persisted_and_unlocks_operations(tmp_path):
    (tmp_path / "compose.yaml").write_text(SOURCE, encoding="utf-8")
    document = ComposeDocument(tmp_path, tmp_path / ".webcontroller")
    runner = FakeRunner()

    async def apply(source, emit):
        emit("partially updated\n")
        raise DeploymentError("daemon disconnected", 503)

    runner.apply = apply
    tasks = TaskManager(document, runner)
    task = await tasks.start(document.read()["version"])
    await tasks.worker
    result = tasks.read(task["id"])
    assert result["state"] == "failed"
    assert "daemon disconnected" in result["output"]
    assert document.source() == SOURCE
    assert not (document.state / "applied.json").exists()
    tasks.available()


def test_unknown_host_and_large_chunked_body_are_rejected(panel):
    client, _, _ = panel
    assert client.get("/", headers={"Host": "unexpected.example"}).status_code == 400
    assert (
        client.post("/api/login", content=iter([b"x" * 1100000, b"x" * 1100000])).status_code == 413
    )


@pytest.mark.asyncio
async def test_resolved_env_file_values_are_filtered_from_logs(tmp_path):
    runner = ComposeRunner(tmp_path, "test-project", verify_container=False)

    async def execute(args, source="", emit=None, timeout=60):
        return json.dumps(
            {
                "services": {
                    "webcontroller": {},
                    "db": {"environment": {"PASSWORD": "from-env-file"}},
                }
            }
        )

    runner.execute = execute
    await runner.validate(SOURCE)
    assert "from-env-file" not in runner.redact("output from-env-file", SOURCE)
