import subprocess
from pathlib import Path

from minisweagent.tools.text import TextTool, execute_text_in_subprocess


def test_replace_text_is_exact_and_preserves_surrounding_bytes(tmp_path: Path):
    path = tmp_path / "example.py"
    path.write_bytes(b"one\r\ntarget\r\nthree\r\n")
    result = TextTool(tmp_path).execute(
        {"tool": "replace_text", "path": "example.py", "old_text": "target", "new_text": "changed"}
    )
    assert result["ok"] is True
    assert result["returncode"] == 0
    assert path.read_bytes() == b"one\r\nchanged\r\nthree\r\n"
    assert "2:" in result["excerpt"]


def test_replace_text_refuses_missing_ambiguous_empty_and_invalid_paths(tmp_path: Path):
    path = tmp_path / "example.txt"
    original = b"same\nsame\n"
    path.write_bytes(original)
    tool = TextTool(tmp_path)
    for old_text, reason in [("missing", "not_found"), ("same", "ambiguous"), ("", "empty_old_text")]:
        result = tool.execute({"tool": "replace_text", "path": "example.txt", "old_text": old_text, "new_text": "x"})
        assert result["ok"] is False
        assert result["reason"] == reason
        assert path.read_bytes() == original
    escaped = tool.execute({"tool": "read_text", "path": "../outside", "start_line": 1, "end_line": 2})
    assert escaped["ok"] is False
    assert "escapes" in escaped["error"]


def test_read_and_create_text_are_bounded_and_create_only(tmp_path: Path):
    path = tmp_path / "source.txt"
    path.write_text("one\ntwo\nthree\n", encoding="utf-8")
    tool = TextTool(tmp_path)
    read = tool.execute({"tool": "read_text", "path": "source.txt", "start_line": 2, "end_line": 3})
    assert read["ok"] is True
    assert read["content"] == "two\nthree\n"
    created = tool.execute({"tool": "create_text", "path": "new.txt", "content": "hello\n"})
    assert created["ok"] is True
    assert (tmp_path / "new.txt").read_text() == "hello\n"
    collision = tool.execute({"tool": "create_text", "path": "new.txt", "content": "overwrite"})
    assert collision["ok"] is False
    assert collision["reason"] == "already_exists"
    assert (tmp_path / "new.txt").read_text() == "hello\n"


def test_read_text_enforces_line_and_character_limits(tmp_path: Path):
    (tmp_path / "large.txt").write_text("12345\n" * 4, encoding="utf-8")
    tool = TextTool(tmp_path, max_read_lines=2, max_read_chars=6)
    too_many_lines = tool.execute({"tool": "read_text", "path": "large.txt", "start_line": 1, "end_line": 3})
    assert too_many_lines["ok"] is False
    assert too_many_lines["reason"] == "line_limit_exceeded"
    too_many_chars = tool.execute({"tool": "read_text", "path": "large.txt", "start_line": 1, "end_line": 2})
    assert too_many_chars["ok"] is False
    assert too_many_chars["reason"] == "character_limit_exceeded"


def test_serialized_helper_uses_the_selected_workspace(tmp_path: Path):
    host_file = tmp_path / "host.txt"
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    def execute(action, cwd="", *, timeout=None):
        completed = subprocess.run(action["command"], shell=True, cwd=cwd, text=True, capture_output=True, timeout=timeout)
        return {"output": completed.stdout, "returncode": completed.returncode, "exception_info": completed.stderr}

    result = execute_text_in_subprocess(
        {"tool": "create_text", "path": "inside.txt", "content": "workspace only"}, execute, cwd=str(workspace)
    )
    assert result["ok"] is True
    assert (workspace / "inside.txt").read_text() == "workspace only"
    assert not host_file.exists()


def test_serialized_helper_reports_missing_runtime():
    def unavailable_execute(action, cwd="", *, timeout=None):
        return {"output": "python3: not found", "returncode": 127, "exception_info": ""}

    result = execute_text_in_subprocess({"tool": "read_text", "path": "a", "start_line": 1, "end_line": 1}, unavailable_execute)
    assert result["reason"] == "text_tool_unavailable"
