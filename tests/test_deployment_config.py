import pytest

from web.deployment.config import ComposeDocument, DeploymentError

SOURCE = """# deployment comment
services:
  webcontroller:
    image: panel:1
    volumes: [state:/state]
  db:
    image: postgres:18 # keep me
    environment: [PASSWORD=secret]
    x-custom: preserved
volumes:
  state:
"""


@pytest.fixture
def document(tmp_path):
    (tmp_path / "compose.yaml").write_text(SOURCE, encoding="utf-8")
    return ComposeDocument(tmp_path, tmp_path / ".webcontroller")


def test_form_patch_keeps_comments_unknown_fields_and_environment_style(document):
    result = document.patch(SOURCE, "db", {"image": "postgres:19"})
    assert "# deployment comment" in result
    assert "# keep me" in result
    assert "x-custom: preserved" in result
    assert "[PASSWORD=secret]" in result
    assert "postgres:19" in result


@pytest.mark.parametrize(
    "source",
    [
        SOURCE.replace("panel:1", "panel:2"),
        SOURCE.replace("state:\n", "state: {external: true}\n"),
        SOURCE + "name: different\n",
    ],
)
def test_panel_and_project_identity_are_protected(document, source):
    with pytest.raises(DeploymentError):
        document.parse(source)


def test_save_conflict_keeps_external_change_and_creates_no_backup(document):
    version = document.read()["version"]
    document.path.write_text(SOURCE + "# external edit\n", encoding="utf-8")
    with pytest.raises(DeploymentError, match="其他"):
        document.save(SOURCE, version)
    assert "# external edit" in document.path.read_text(encoding="utf-8")
    assert document.backups() == []


def test_save_and_restore_preserve_exact_original(document):
    updated = SOURCE.replace("postgres:18", "postgres:19")
    document.save(updated, document.read()["version"])
    backup = document.backups()[0]["id"]
    document.save(document.backup_source(backup), document.read()["version"])
    assert document.path.read_text(encoding="utf-8") == SOURCE


def test_failed_replace_keeps_original(document, monkeypatch):
    import os

    original_replace = os.replace

    def replace(source, target):
        if target == document.path:
            raise OSError("disk error")
        original_replace(source, target)

    monkeypatch.setattr("web.deployment.config.os.replace", replace)
    with pytest.raises(OSError):
        document.save(SOURCE.replace("postgres:18", "postgres:19"), document.read()["version"])
    assert document.path.read_text(encoding="utf-8") == SOURCE
    assert len(document.backups()) == 1


def test_invalid_yaml_and_path_traversal_are_rejected(document):
    with pytest.raises(DeploymentError):
        document.parse("services: [")
    with pytest.raises(DeploymentError):
        document.backup_source("../compose.yaml")


def test_protection_survives_external_edit_and_restart(document):
    document.path.write_text(SOURCE.replace("panel:1", "panel:changed"), encoding="utf-8")
    restarted = ComposeDocument(document.root, document.state)
    with pytest.raises(DeploymentError):
        restarted.parse(restarted.path.read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    "field,value",
    [
        ("depends_on", 5),
        ("volumes", {}),
        ("environment", True),
        ("healthcheck", []),
        ("ports", "3080:3080"),
    ],
)
def test_invalid_form_field_types_return_validation_error(document, field, value):
    with pytest.raises(DeploymentError):
        document.patch(SOURCE, "db", {field: value})


def test_nested_comments_survive_environment_form_edit(document):
    source = SOURCE.replace(
        "environment: [PASSWORD=secret]",
        "environment:\n      PASSWORD: secret # password comment\n      KEEP: value # keep comment",
    )
    result = document.patch(
        source, "db", {"environment": {"PASSWORD": "new-password", "KEEP": "value"}}
    )
    assert "# password comment" in result
    assert "# keep comment" in result
    assert "new-password" in result


@pytest.mark.parametrize("field", ["pid", "ipc", "network_mode"])
def test_implicit_panel_dependency_is_rejected(document, field):
    source = SOURCE.replace("x-custom: preserved", f"{field}: service:webcontroller")
    with pytest.raises(DeploymentError):
        document.parse(source)
