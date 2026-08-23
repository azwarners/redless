"""Safe, exact UTF-8 text operations for tool-capable models."""

import base64
import inspect
import json
import os
import shlex
import tempfile
import textwrap
from pathlib import Path
from typing import Any


class TextTool:
    def __init__(
        self,
        root: str | Path,
        *,
        excerpt_chars: int = 1200,
        max_read_lines: int = 200,
        max_read_chars: int = 12000,
    ):
        self.root = Path(root).resolve()
        self.excerpt_chars = excerpt_chars
        self.max_read_lines = max_read_lines
        self.max_read_chars = max_read_chars

    def execute(self, action: dict[str, Any]) -> dict[str, Any]:
        tool = action.get("tool")
        try:
            if tool not in {"replace_text", "read_text", "create_text"}:
                raise ValueError(f"unknown text tool: {tool}")
            result = getattr(self, tool)(action)
        except (OSError, UnicodeError, ValueError) as e:
            reason = {
                UnicodeDecodeError: "decode_error",
                FileNotFoundError: "not_found",
                PermissionError: "inaccessible",
                IsADirectoryError: "is_directory",
            }.get(type(e), type(e).__name__)
            result = {"ok": False, "reason": reason, "error": str(e)}
        result["summary"] = self._summary(result)
        return {"output": json.dumps(result, ensure_ascii=False), "returncode": 0 if result["ok"] else 1, "exception_info": "", **result}

    def replace_text(self, action: dict[str, Any]) -> dict[str, Any]:
        path = self._resolve(action["path"])
        old_text = action["old_text"]
        if not old_text:
            return {"ok": False, "reason": "empty_old_text", "matches": 0}
        data = path.read_bytes()
        content = data.decode("utf-8")
        matches = content.count(old_text)
        if matches != 1:
            return {"ok": False, "reason": "not_found" if matches == 0 else "ambiguous", "matches": matches}
        new_content = content.replace(old_text, action["new_text"], 1)
        self._atomic_write(path, new_content.encode("utf-8"))
        start = new_content.index(action["new_text"]) if action["new_text"] else max(0, content.index(old_text))
        return {
            "ok": True,
            "path": str(path),
            "matches": 1,
            "excerpt": self._excerpt(new_content, start),
        }

    def read_text(self, action: dict[str, Any]) -> dict[str, Any]:
        path = self._resolve(action["path"])
        start_line = action["start_line"]
        end_line = action["end_line"]
        if start_line > end_line:
            raise ValueError("start_line must be less than or equal to end_line")
        if end_line - start_line + 1 > self.max_read_lines:
            return {
                "ok": False,
                "reason": "line_limit_exceeded",
                "max_lines": self.max_read_lines,
            }
        selected: list[str] = []
        with path.open("rb") as file:
            for line_number, raw_line in enumerate(file, 1):
                if line_number > end_line:
                    break
                if line_number >= start_line:
                    selected.append(raw_line.decode("utf-8"))
                    if sum(map(len, selected)) > self.max_read_chars:
                        return {
                            "ok": False,
                            "reason": "character_limit_exceeded",
                            "max_chars": self.max_read_chars,
                        }
        content = "".join(selected)
        return {
            "ok": True,
            "path": str(path),
            "start_line": start_line,
            "end_line": start_line + len(selected) - 1,
            "content": content,
        }

    def create_text(self, action: dict[str, Any]) -> dict[str, Any]:
        path = self._resolve(action["path"])
        if path.exists():
            return {"ok": False, "reason": "already_exists", "path": str(path)}
        if not path.parent.is_dir():
            return {"ok": False, "reason": "parent_not_found", "path": str(path)}
        self._atomic_write(path, action["content"].encode("utf-8"), exclusive=True)
        return {"ok": True, "path": str(path), "bytes": len(action["content"].encode("utf-8"))}

    def _resolve(self, raw_path: str) -> Path:
        path = Path(raw_path)
        if path.is_absolute():
            raise ValueError("absolute paths are not permitted")
        resolved = (self.root / path).resolve()
        if resolved != self.root and self.root not in resolved.parents:
            raise ValueError("path escapes the environment working directory")
        if resolved.is_dir():
            raise IsADirectoryError(str(resolved))
        return resolved

    @staticmethod
    def _atomic_write(path: Path, data: bytes, *, exclusive: bool = False) -> None:
        fd, temp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
        temp_path = Path(temp_name)
        try:
            with os.fdopen(fd, "wb") as file:
                file.write(data)
                file.flush()
                os.fsync(file.fileno())
            if exclusive and path.exists():
                raise FileExistsError(str(path))
            temp_path.replace(path)
        finally:
            temp_path.unlink(missing_ok=True)

    def _excerpt(self, content: str, position: int) -> str:
        start = max(0, position - 200)
        end = min(len(content), position + self.excerpt_chars)
        excerpt = content[start:end]
        line_no = content[:start].count("\n") + 1
        return "".join(f"{line_no + i}: {line}\n" for i, line in enumerate(excerpt.splitlines()))[: self.excerpt_chars]

    @staticmethod
    def _summary(result: dict[str, Any]) -> str:
        if result["ok"]:
            return "text operation succeeded"
        return f"text operation failed: {result.get('reason', 'error')}"


def execute_text_in_subprocess(
    action: dict[str, Any], execute: Any, *, cwd: str = "", timeout: int | None = None
) -> dict[str, Any]:
    """Run the shared text implementation inside an environment workspace."""
    encoded_action = base64.b64encode(json.dumps(action, separators=(",", ":")).encode()).decode()
    source = textwrap.dedent(inspect.getsource(TextTool))
    script = (
        "import base64,json,os,tempfile\n"
        "from pathlib import Path\n"
        "from typing import Any\n"
        f"{source}\n"
        f"action=json.loads(base64.b64decode({encoded_action!r}))\n"
        "print(TextTool(os.getcwd()).execute(action)['output'])\n"
    )
    result = execute({"command": f"python3 -c {shlex.quote(script)}"}, cwd=cwd, timeout=timeout)
    try:
        payload = json.loads(result.get("output", ""))
    except (TypeError, json.JSONDecodeError):
        return {
            "output": "",
            "returncode": 1,
            "exception_info": "",
            "ok": False,
            "reason": "text_tool_unavailable",
            "summary": "text tool unavailable in environment",
        }
    return {
        "output": json.dumps(payload, ensure_ascii=False),
        "returncode": 0 if payload.get("ok") else 1,
        "exception_info": "",
        **payload,
    }
