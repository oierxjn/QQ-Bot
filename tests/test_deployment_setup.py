import json
import os
import sys
from types import SimpleNamespace

import pytest

from tests.test_deployment_config import SOURCE
from web.deployment import default_source_root
from web.deployment.auth import Authentication
from web.deployment.config import DeploymentError
from web.deployment.setup import discover_project, initialize, initialize_source, main
from web.deployment.toml_config import CONFIG_NAMES


def test_initialization_preserves_existing_env_and_credentials(tmp_path):
    (tmp_path / "compose.yaml").write_text(SOURCE, encoding="utf-8")
    (tmp_path / ".env").write_text(
        "# keep comment\nOTHER=value\nCOMPOSE_PROJECT_NAME=old\n", encoding="utf-8"
    )
    initialize(tmp_path, "existing-project", "test-password")
    credential_path = tmp_path / ".webcontroller" / "credentials.json"
    credentials = credential_path.read_text(encoding="utf-8")
    assert "test-password" not in credentials
    env = (tmp_path / ".env").read_text(encoding="utf-8")
    assert "# keep comment" in env and "OTHER=value" in env
    assert "COMPOSE_PROJECT_NAME='existing-project'" in env
    initialize(tmp_path, "existing-project")
    assert credential_path.read_text(encoding="utf-8") == credentials
    settings = json.loads(
        (tmp_path / ".webcontroller" / "settings.json").read_text(encoding="utf-8")
    )
    assert settings["root"] == str(tmp_path.resolve())
    with pytest.raises(DeploymentError):
        initialize(tmp_path, "different-project")


def test_project_detection_uses_existing_container_labels(tmp_path, monkeypatch):
    (tmp_path / "compose.yaml").write_text(SOURCE, encoding="utf-8")
    monkeypatch.setattr(
        "web.deployment.setup.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(stdout=f"original\t{tmp_path}\n"),
    )
    assert discover_project(tmp_path) == "original"
    with pytest.raises(DeploymentError):
        discover_project(tmp_path, "different")


def test_ambiguous_project_requires_explicit_existing_project(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "web.deployment.setup.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(stdout=f"first\t{tmp_path}\nsecond\t{tmp_path}\n"),
    )
    with pytest.raises(DeploymentError):
        discover_project(tmp_path)
    assert discover_project(tmp_path, "second") == "second"


def test_new_deployment_honors_env_project_name(tmp_path, monkeypatch):
    (tmp_path / "compose.yaml").write_text(SOURCE, encoding="utf-8")
    (tmp_path / ".env").write_text("COMPOSE_PROJECT_NAME='chosen'\n", encoding="utf-8")
    monkeypatch.setattr(
        "web.deployment.setup.subprocess.run", lambda *args, **kwargs: SimpleNamespace(stdout="")
    )
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
    monkeypatch.setattr(sys, "argv", ["setup", "--mode", "source"])
    monkeypatch.setattr("web.deployment.setup.default_source_root", lambda: tmp_path)
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
