import subprocess

from typer.testing import CliRunner

from minisweagent.run.workspace import app


def test_init_creates_a_safe_local_model_workspace(tmp_path):
    path = tmp_path / "workspace"
    result = CliRunner().invoke(
        app,
        ["init", str(path), "--model", "local-model", "--api-base", "http://server:8080/v1"],
    )

    assert result.exit_code == 0
    assert subprocess.check_output(["git", "-C", path, "rev-parse", "--is-inside-work-tree"], text=True).strip() == "true"
    assert "model_name: local-model" in (path / ".mini-swe-agent-slow" / "llama-local.yaml").read_text()
    assert "api_base: http://server:8080/v1" in (path / ".mini-swe-agent-slow" / "llama-local.yaml").read_text()
    assert ".mini-swe-agent-slow/" in (path / ".git" / "info" / "exclude").read_text()
    assert "MSWEA_CONFIGURED=true mini-slow" in result.output


def test_init_refuses_an_existing_directory(tmp_path):
    path = tmp_path / "workspace"
    path.mkdir()

    result = CliRunner().invoke(app, ["init", str(path), "--model", "local-model"])

    assert result.exit_code != 0
    assert "Workspace already exists" in result.output
