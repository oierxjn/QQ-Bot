import json
import os
import sys

import pytest

from tests.test_deployment_config import SOURCE
from web.deployment import default_source_root
from web.deployment.__main__ import main
from web.deployment.auth import Authentication
from web.deployment.config import DeploymentError
from web.deployment.setup import (
    discover_project,
    initialize,
    initialize_source,
)
from web.deployment.toml_config import CONFIG_NAMES


def test_initialization_preserves_existing_files_and_credentials(tmp_path):
    (tmp_path / "compose.yaml").write_text(SOURCE, encoding="utf-8")
    (tmp_path / ".env").write_text("# keep comment\nOTHER=value\n", encoding="utf-8")
    initialize(tmp_path, "existing-project", "test-password")
    credential_path = tmp_path / ".webcontroller" / "credentials.json"
    credentials = credential_path.read_text(encoding="utf-8")
    assert "test-password" not in credentials
    assert (tmp_path / ".env").read_text(encoding="utf-8") == "# keep comment\nOTHER=value\n"
    initialize(tmp_path, "existing-project")
    assert credential_path.read_text(encoding="utf-8") == credentials
    settings = json.loads(
        (tmp_path / ".webcontroller" / "settings.json").read_text(encoding="utf-8")
    )
    assert settings == {
        "root": str(tmp_path.resolve()),
        "project": "existing-project",
    }
    with pytest.raises(DeploymentError):
        initialize(tmp_path, "different-project")


def test_project_detection_reads_compose_name(tmp_path):
    (tmp_path / "compose.yaml").write_text(SOURCE + "name: from-compose\n", encoding="utf-8")
    assert discover_project(tmp_path) == "from-compose"


def test_explicit_project_name_overrides_detection(tmp_path):
    (tmp_path / "compose.yaml").write_text(SOURCE, encoding="utf-8")
    assert discover_project(tmp_path, "override") == "override"
    with pytest.raises(DeploymentError):
        discover_project(tmp_path, "Invalid Name!")


def test_project_detection_requires_compose_file(tmp_path):
    with pytest.raises(DeploymentError):
        discover_project(tmp_path)


def test_new_deployment_honors_env_project_name(tmp_path):
    (tmp_path / "compose.yaml").write_text(SOURCE + "name: from-compose\n", encoding="utf-8")
    (tmp_path / ".env").write_text("COMPOSE_PROJECT_NAME='chosen'\n", encoding="utf-8")
    assert discover_project(tmp_path) == "chosen"


def test_default_source_root_points_at_checkout():
    root = default_source_root()
    assert (root / "pyproject.toml").is_file()
    assert (root / "web" / "deployment").is_dir()


def test_source_main_defaults_to_package_root(tmp_path, monkeypatch):
    configs = tmp_path / "configs"
    configs.mkdir()
    for name in CONFIG_NAMES:
        (configs / f"{name}.template").write_text("", encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["web.deployment", "--init"])
    monkeypatch.setattr("web.deployment.runtime.default_source_root", lambda: tmp_path)
    main()
    settings = json.loads(
        (tmp_path / ".webcontroller" / "settings.json").read_text(encoding="utf-8")
    )
    assert settings == {"root": str(tmp_path.resolve())}
    initial = tmp_path / ".webcontroller" / "initial-password"
    assert len(initial.read_text(encoding="utf-8").strip()) >= 12


def test_initial_password_file_lifecycle(tmp_path):
    configs = tmp_path / "configs"
    configs.mkdir()
    for name in CONFIG_NAMES:
        (configs / f"{name}.template").write_text("", encoding="utf-8")
    initialize_source(tmp_path)
    initial = tmp_path / ".webcontroller" / "initial-password"
    password = initial.read_text(encoding="utf-8").strip()
    assert len(password) >= 12
    if os.name != "nt":
        assert initial.stat().st_mode & 0o777 == 0o600
    credentials = json.loads(
        (tmp_path / ".webcontroller" / "credentials.json").read_text(encoding="utf-8")
    )
    Authentication(credentials).login(password)
    initialize_source(tmp_path)
    assert initial.read_text(encoding="utf-8") == password + "\n"


def test_compose_init_generates_initial_password(tmp_path):
    (tmp_path / "compose.yaml").write_text(SOURCE, encoding="utf-8")
    initialize(tmp_path, "generated-project")
    initial = tmp_path / ".webcontroller" / "initial-password"
    assert len(initial.read_text(encoding="utf-8").strip()) >= 12


class _ImmediateServer:
    def __init__(self, config):
        pass

    async def serve(self):
        pass


def test_serve_auto_initializes(tmp_path, monkeypatch):
    import sys as _sys

    from web.deployment import runtime

    configs = tmp_path / "configs"
    configs.mkdir()
    for name in CONFIG_NAMES:
        (configs / f"{name}.template").write_text("", encoding="utf-8")
    recorded = {}

    async def fake_supervise(stop, command, cwd, **kwargs):
        recorded["command"] = command
        recorded["cwd"] = cwd
        stop.set()

    monkeypatch.setattr(runtime, "supervise_bot", fake_supervise)
    monkeypatch.setattr("web.deployment.runtime.uvicorn.Server", _ImmediateServer)
    monkeypatch.setattr(_sys, "argv", ["web.deployment", "--root", str(tmp_path)])
    main()
    assert (tmp_path / ".webcontroller" / "settings.json").is_file()
    assert (tmp_path / ".webcontroller" / "initial-password").is_file()
    assert recorded["command"] == [_sys.executable, "main.py"]
    assert recorded["cwd"] == tmp_path.resolve()


def test_compose_serve_auto_initializes(tmp_path, monkeypatch):
    import sys

    from web.deployment import runtime

    (tmp_path / "compose.yaml").write_text(SOURCE, encoding="utf-8")

    async def no_supervise(*args, **kwargs):
        pass

    monkeypatch.setattr(runtime, "supervise_bot", no_supervise)
    monkeypatch.setattr(runtime, "PANEL_DEPLOY_MOUNT", str(tmp_path))
    monkeypatch.setattr("web.deployment.runtime.uvicorn.Server", _ImmediateServer)
    monkeypatch.setattr(sys, "argv", ["web.deployment"])
    main()
    settings = json.loads(
        (tmp_path / ".webcontroller" / "settings.json").read_text(encoding="utf-8")
    )
    assert settings == {
        "root": str(tmp_path.resolve()),
        "project": runtime.discover_project(tmp_path),
    }
    assert (tmp_path / ".webcontroller" / "initial-password").is_file()
    assert (tmp_path / ".webcontroller" / "protected.json").is_file()


def test_compose_serve_supervises_entrypoint_chain(tmp_path, monkeypatch):
    import sys

    from web.deployment import runtime

    (tmp_path / "compose.yaml").write_text(SOURCE, encoding="utf-8")
    recorded = {}

    async def fake_supervise(stop, command, cwd, **kwargs):
        recorded["command"] = command
        recorded["cwd"] = cwd
        stop.set()

    monkeypatch.setattr(runtime, "supervise_bot", fake_supervise)
    monkeypatch.setattr(runtime, "PANEL_DEPLOY_MOUNT", str(tmp_path))
    monkeypatch.setattr("web.deployment.runtime.uvicorn.Server", _ImmediateServer)
    monkeypatch.setattr(sys, "argv", ["web.deployment"])
    main()
    assert recorded["command"] == ["bash", "scripts/entrypoint.sh", "python3", "main.py"]
    assert recorded["cwd"] == runtime.default_source_root().resolve()


@pytest.mark.asyncio
async def test_supervise_bot_restarts_then_stops(tmp_path):
    import asyncio
    import sys

    from web.deployment.runtime import supervise_bot

    stop = asyncio.Event()
    marker = tmp_path / "marker"
    command = [sys.executable, "-c", f"open({str(marker)!r}, 'a').write('x')"]
    task = asyncio.create_task(
        supervise_bot(stop, command, tmp_path, backoff_start=0.05, backoff_cap=0.1)
    )
    try:
        for _ in range(200):
            if marker.is_file() and len(marker.read_text(encoding="utf-8")) >= 3:
                break
            await asyncio.sleep(0.05)
        assert marker.is_file() and len(marker.read_text(encoding="utf-8")) >= 3, (
            "supervisor did not restart the child"
        )
    finally:
        stop.set()
        await asyncio.wait_for(task, 10)


@pytest.mark.asyncio
async def test_supervise_bot_terminates_child_on_stop(tmp_path):
    import asyncio
    import sys

    from web.deployment.runtime import supervise_bot

    stop = asyncio.Event()
    command = [sys.executable, "-c", "import time; time.sleep(30)"]
    task = asyncio.create_task(supervise_bot(stop, command, tmp_path, backoff_start=0.05))
    await asyncio.sleep(0.5)
    stop.set()
    await asyncio.wait_for(task, 10)


@pytest.mark.asyncio
async def test_supervise_bot_resets_backoff_after_stable_run(tmp_path):
    import asyncio
    import sys
    import time

    from web.deployment.runtime import supervise_bot

    stop = asyncio.Event()
    restarts = tmp_path / "restarts"
    # First child exits immediately; the second sleeps long enough to count as
    # stable (reset_after), so the next crash must restart at backoff_start.
    command = [
        sys.executable,
        "-c",
        (
            "import sys, time\n"
            f"p = {str(restarts)!r}\n"
            "open(p, 'a').write('x')\n"
            "if len(open(p).read()) > 1:\n"
            "    time.sleep(1)\n"
            "sys.exit(1)\n"
        ),
    ]
    task = asyncio.create_task(
        supervise_bot(stop, command, tmp_path, backoff_start=0.05, backoff_cap=5.0, reset_after=0.5)
    )
    try:
        deadline = time.monotonic() + 20
        while True:
            if restarts.is_file() and len(restarts.read_text(encoding="utf-8")) >= 3:
                break
            assert time.monotonic() < deadline, "supervisor did not restart after stable run"
            await asyncio.sleep(0.05)
    finally:
        stop.set()
        await asyncio.wait_for(task, 10)


def test_detect_runtime_uses_deploy_mount(tmp_path, monkeypatch):
    from web.deployment import runtime

    (tmp_path / "compose.yaml").write_text(SOURCE, encoding="utf-8")
    monkeypatch.setattr(runtime, "PANEL_DEPLOY_MOUNT", str(tmp_path))
    assert runtime.detect_runtime() == (tmp_path.resolve(), "compose")
    monkeypatch.setattr(runtime, "PANEL_DEPLOY_MOUNT", str(tmp_path / "absent"))
    assert runtime.detect_runtime(str(tmp_path)) == (tmp_path.resolve(), "source")
    root, mode = runtime.detect_runtime()
    assert mode == "source"
    assert (root / "pyproject.toml").is_file()


def test_source_init_with_explicit_password(tmp_path):
    configs = tmp_path / "configs"
    configs.mkdir()
    for name in CONFIG_NAMES:
        (configs / f"{name}.template").write_text("", encoding="utf-8")
    initialize_source(tmp_path, "chosen-pw")
    assert not (tmp_path / ".webcontroller" / "initial-password").exists()
    credentials = json.loads(
        (tmp_path / ".webcontroller" / "credentials.json").read_text(encoding="utf-8")
    )
    Authentication(credentials).login("chosen-pw")


def test_short_password_is_accepted(tmp_path):
    (tmp_path / "compose.yaml").write_text(SOURCE, encoding="utf-8")
    initialize(tmp_path, "short-password-project", "abc")
    credentials = json.loads(
        (tmp_path / ".webcontroller" / "credentials.json").read_text(encoding="utf-8")
    )
    Authentication(credentials).login("abc")


def test_cli_flag_validation(monkeypatch):
    def run(*argv):
        monkeypatch.setattr(sys, "argv", ["web.deployment", *argv])
        main()

    with pytest.raises(SystemExit):
        run("--password", "x")
    with pytest.raises(SystemExit):
        run("--init", "--bg")
    with pytest.raises(SystemExit):
        run("--init", "--reset-password")
