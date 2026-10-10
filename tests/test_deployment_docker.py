"""Isolated Linux Docker integration; never targets the real deployment."""

import asyncio
import io
import os
import shutil
import subprocess
import sys
import uuid

import httpx
import pytest

from web.deployment.config import ComposeDocument, yaml_parser
from web.deployment.runner import ComposeRunner
from web.deployment.setup import initialize

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_WEB_DOCKER_TESTS") != "1"
    or sys.platform != "linux"
    or not shutil.which("docker"),
    reason="Requires Linux Docker and RUN_WEB_DOCKER_TESTS=1",
)


@pytest.mark.asyncio
async def test_real_compose_relative_bind_env_and_recreation(tmp_path):
    project = "paneltest-" + uuid.uuid4().hex
    data = tmp_path / "data"
    data.mkdir()
    (data / "value.txt").write_text("exists", encoding="utf-8")
    (tmp_path / ".env").write_text("CHECK_VALUE=private-integration-value\n", encoding="utf-8")
    source = """services:
  webcontroller:
    image: alpine:3.21
    command: [sleep, '300']
  worker:
    image: alpine:3.21
    command: [sleep, '300']
    environment:
      CHECK_VALUE: ${CHECK_VALUE}
      REVISION: '1'
    volumes:
      - ./data:/data:ro
      - store:/store
    healthcheck:
      test: [CMD, test, -f, /data/value.txt]
      interval: 1s
      timeout: 1s
      retries: 3
  sidecar:
    image: alpine:3.21
    command: [sleep, '300']
    profiles: [extra]
volumes:
  store:
"""
    (tmp_path / "compose.yaml").write_text(source, encoding="utf-8")
    document = ComposeDocument(tmp_path, tmp_path / ".webcontroller")
    runner = ComposeRunner(tmp_path, project, verify_container=False)
    prefix = [
        "docker",
        "compose",
        "--project-directory",
        str(tmp_path),
        "-p",
        project,
        "-f",
        str(document.path),
    ]

    def command(*args):
        return subprocess.run(
            prefix + list(args), check=True, capture_output=True, text=True
        ).stdout.strip()

    try:
        resolved = await runner.validate(source)
        assert (
            resolved["services"]["worker"]["environment"]["CHECK_VALUE"]
            == "private-integration-value"
        )
        assert resolved["services"]["worker"]["volumes"][0]["source"] == str(data)
        # The panel only validates config; bringing services up is a host-side action.
        command("up", "-d", "--wait", "--wait-timeout", "60", "worker")
        services_up = command("ps", "--all", "--format", "{{.Service}}").split()
        assert "worker" in services_up
        assert "sidecar" not in services_up, "profiled service must not be started"
    finally:
        # Only the unique temporary project created above is removed.
        command("down", "--volumes", "--remove-orphans")


@pytest.mark.asyncio
async def test_actual_panel_container_login_and_config_save(tmp_path):
    project = "paneltest-" + uuid.uuid4().hex
    image = os.environ.get("WEB_PANEL_TEST_IMAGE", "theresa-webcontroller:test")
    config = {
        "services": {
            "webcontroller": {
                "image": image,
                "ports": ["127.0.0.1::7001"],
                "volumes": [f"{tmp_path}:/deploy", "/var/run/docker.sock:/var/run/docker.sock"],
            },
            "worker": {
                "image": "alpine:3.21",
                "command": ["sleep", "300"],
                "environment": {"REVISION": "1"},
            },
        }
    }
    output = io.StringIO()
    yaml_parser().dump(config, output)
    (tmp_path / "compose.yaml").write_text(output.getvalue(), encoding="utf-8")
    initialize(tmp_path, project, "test-password")
    prefix = [
        "docker",
        "compose",
        "--project-directory",
        str(tmp_path),
        "-p",
        project,
        "-f",
        str(tmp_path / "compose.yaml"),
    ]

    def command(*args):
        return subprocess.run(
            prefix + list(args), check=True, capture_output=True, text=True
        ).stdout.strip()

    try:
        command("up", "-d", "webcontroller")
        address = command("port", "webcontroller", "7001")
        async with httpx.AsyncClient(base_url="http://" + address, timeout=90) as client:
            for _attempt in range(100):
                try:
                    response = await client.post("/api/login", json={"password": "test-password"})
                    if response.status_code == 200:
                        break
                except httpx.TransportError:
                    pass
                await asyncio.sleep(0.2)
            else:
                raise AssertionError("Panel container did not become ready")
            client.headers["X-CSRF-Token"] = response.json()["csrf"]
            document_payload = (await client.get("/api/compose")).json()
            draft = (
                await client.post(
                    "/api/compose/patch",
                    json={
                        "source": document_payload["source"],
                        "service": "worker",
                        "changes": {"environment": {"REVISION": "2"}},
                    },
                )
            ).json()
            saved = await client.put(
                "/api/compose",
                json={"source": draft["source"], "version": document_payload["version"]},
            )
            assert saved.status_code == 200, saved.text
            assert saved.json()["services"]["worker"]["environment"]["REVISION"] == "2"
    finally:
        command("down", "--volumes", "--remove-orphans")
