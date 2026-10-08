import json
import shutil
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from tests.test_deployment_api import login
from tests.test_deployment_api import panel as panel_fixture
from web.deployment.__main__ import build_app
from web.deployment.config import DeploymentError
from web.deployment.setup import initialize_source

compose_panel = panel_fixture


@pytest.fixture
def source_root(tmp_path):
    (tmp_path / "configs").mkdir()
    for template in Path("configs").glob("*.toml.template"):
        shutil.copyfile(template, tmp_path / "configs" / template.name)
    return tmp_path


def test_source_init_without_docker_preserves_existing(source_root):
    existing = source_root / "configs" / "plugins.toml"
    existing.write_text("broken[", encoding="utf-8")
    with patch("subprocess.run", side_effect=AssertionError("Docker must not be used")):
        initialize_source(source_root, "test-password")
        credentials = (source_root / ".webcontroller/credentials.json").read_bytes()
        initialize_source(source_root)
        app = build_app(source_root, "source")
    assert existing.read_text(encoding="utf-8") == "broken["
    assert (source_root / ".webcontroller/credentials.json").read_bytes() == credentials
    assert not (source_root / ".env").exists()
    with TestClient(app, base_url="http://localhost") as client:
        assert client.get("/api/configs").status_code == 401
        headers = login(client)
        assert client.get("/api/capabilities").json()["mode"] == "source"
        assert client.get("/api/compose").status_code == 404
        original = client.get("/api/configs/plugins.toml").json()
        assert original["error"]
        body = {"source": "[Example]\nenable = false\n", "version": original["version"]}
        assert client.put("/api/configs/plugins.toml", json=body).status_code == 403
        saved = client.put("/api/configs/plugins.toml", json=body, headers=headers)
        assert saved.status_code == 200
        assert (
            client.put("/api/configs/plugins.toml", json=body, headers=headers).status_code == 409
        )
        assert client.get("/api/configs/credentials.json").status_code == 404
        backups = client.get("/api/configs/plugins.toml/backups").json()["backups"]
        assert len(backups) == 1
        # Broken originals remain downloadable but cannot replace valid configuration.
        assert (
            client.post(
                f"/api/configs/plugins.toml/backups/{backups[0]['id']}/restore",
                json={"version": saved.json()["version"]},
                headers=headers,
            ).status_code
            == 422
        )


def test_mode_conflict(source_root):
    initialize_source(source_root, "test-password")
    with pytest.raises(RuntimeError):
        build_app(source_root, "compose", "test")
    settings = source_root / ".webcontroller/settings.json"
    settings.write_text(
        json.dumps({"root": str(source_root.resolve()), "project": "test"}), encoding="utf-8"
    )
    with pytest.raises(DeploymentError):
        initialize_source(source_root)


def test_source_cli_serves_without_compose(source_root):
    import socket
    import subprocess
    import sys
    import time

    import httpx

    initialize_source(source_root, "test-password")
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "web.deployment",
            "--mode",
            "source",
            "--root",
            str(source_root),
            "--port",
            str(port),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 10
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", trust_env=False) as client:
            while True:
                assert process.poll() is None, "Source CLI exited unexpectedly"
                try:
                    response = client.get("/")
                    break
                except httpx.ConnectError:
                    assert time.monotonic() < deadline, "Source CLI did not start"
                    time.sleep(0.05)
            assert response.status_code == 200
            assert client.post("/api/login", json={"password": "test-password"}).status_code == 200
            assert client.get("/api/capabilities").json()["mode"] == "source"
            assert client.get("/api/configs/bot.toml").status_code == 200
    finally:
        process.terminate()
        process.wait(timeout=10)


def test_compose_shares_config_and_operation_lock(compose_panel):
    client, document, runner = compose_panel
    headers = login(client)
    body = client.get("/api/configs/groups.toml").json()
    body["source"] = '["123"]\nExample = true\n'
    assert client.get("/api/capabilities").json()["compose"]
    assert client.put("/api/configs/groups.toml", json=body, headers=headers).status_code == 200
    tasks = client.app.state.tasks
    client.portal.call(tasks.lock.acquire)
    try:
        current = client.get("/api/configs/groups.toml").json()
        assert (
            client.put("/api/configs/groups.toml", json=current, headers=headers).status_code == 409
        )
    finally:
        client.portal.call(tasks.lock.release)
