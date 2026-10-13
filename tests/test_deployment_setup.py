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
    adopt_mode,
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
        "mode": "compose",
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
    monkeypatch.setattr(sys, "argv", ["web.deployment", "--mode", "source", "--init"])
    monkeypatch.setattr("web.deployment.__main__.default_source_root", lambda: tmp_path)
    main()
    settings = json.loads(
        (tmp_path / ".webcontroller" / "settings.json").read_text(encoding="utf-8")
    )
    assert settings == {"root": str(tmp_path.resolve()), "mode": "source"}
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


def test_adopt_mode_migrates_identity(tmp_path):
    configs = tmp_path / "configs"
    configs.mkdir()
    for name in CONFIG_NAMES:
        (configs / f"{name}.template").write_text("", encoding="utf-8")
    (tmp_path / "compose.yaml").write_text(SOURCE + "name: adopted\n", encoding="utf-8")
    initialize_source(tmp_path, "test-password")
    credentials = (tmp_path / ".webcontroller" / "credentials.json").read_bytes()
    adopt_mode(tmp_path, "compose")
    settings = json.loads(
        (tmp_path / ".webcontroller" / "settings.json").read_text(encoding="utf-8")
    )
    assert settings == {
        "root": str(tmp_path.resolve()),
        "project": "adopted",
        "mode": "compose",
    }
    assert (tmp_path / ".webcontroller" / "credentials.json").read_bytes() == credentials
    adopt_mode(tmp_path, "source")
    settings = json.loads(
        (tmp_path / ".webcontroller" / "settings.json").read_text(encoding="utf-8")
    )
    assert settings == {"root": str(tmp_path.resolve()), "mode": "source"}
    elsewhere = tmp_path.parent / "adopt-elsewhere"
    (elsewhere / ".webcontroller").mkdir(parents=True)
    (elsewhere / ".webcontroller" / "settings.json").write_text(
        json.dumps({"root": str(tmp_path.resolve()), "mode": "source"}), encoding="utf-8"
    )
    with pytest.raises(DeploymentError):
        adopt_mode(elsewhere, "source")
    with pytest.raises(DeploymentError):
        adopt_mode(tmp_path.parent / "never-initialized", "source")


def test_serve_auto_initializes(tmp_path, monkeypatch):
    configs = tmp_path / "configs"
    configs.mkdir()
    for name in CONFIG_NAMES:
        (configs / f"{name}.template").write_text("", encoding="utf-8")
    monkeypatch.setattr(
        sys, "argv", ["web.deployment", "--mode", "source", "--root", str(tmp_path)]
    )
    monkeypatch.setattr("web.deployment.__main__.uvicorn.run", lambda *args, **kwargs: None)
    main()
    assert (tmp_path / ".webcontroller" / "settings.json").is_file()
    assert (tmp_path / ".webcontroller" / "initial-password").is_file()


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
        run("--mode", "source", "--init", "--bg")
    with pytest.raises(SystemExit):
        run("--mode", "source", "--init", "--reset-password")
