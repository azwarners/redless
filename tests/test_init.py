"""Tests for minisweagent.__init__."""

import os
import subprocess
import sys


def test_startup_banner_survives_non_utf8_stdout(tmp_path):
    """Importing the package must not crash when stdout can't encode the startup banner (e.g. Windows cp1252)."""
    env = {
        **os.environ,
        "PYTHONIOENCODING": "cp1252",
        "MSWEA_SILENT_STARTUP": "",
        "MSWEA_GLOBAL_CONFIG_DIR": str(tmp_path),
    }
    result = subprocess.run([sys.executable, "-c", "import minisweagent"], capture_output=True, text=True, env=env)
    assert result.returncode == 0, result.stderr


def test_default_global_config_is_fork_owned(tmp_path):
    env = {
        **os.environ,
        "HOME": str(tmp_path),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "MSWEA_SILENT_STARTUP": "1",
    }
    result = subprocess.run(
        [sys.executable, "-c", "from minisweagent import global_config_dir; print(global_config_dir)"],
        capture_output=True,
        text=True,
        env=env,
        check=True,
    )
    assert result.stdout.strip().endswith("redless")
    assert "mini-swe-agent\n" not in result.stdout


def test_explicit_global_config_directory_wins(tmp_path):
    explicit = tmp_path / "explicit"
    env = {
        **os.environ,
        "HOME": str(tmp_path / "home"),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "MSWEA_GLOBAL_CONFIG_DIR": str(explicit),
        "MSWEA_SILENT_STARTUP": "1",
    }
    result = subprocess.run(
        [sys.executable, "-c", "from minisweagent import global_config_dir; print(global_config_dir)"],
        capture_output=True,
        text=True,
        env=env,
        check=True,
    )
    assert result.stdout.strip() == str(explicit)
