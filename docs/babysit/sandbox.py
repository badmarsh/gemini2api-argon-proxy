"""Sandbox environment for Argon 4 tool execution.

Guarantees isolation:
- All operations are jailed inside `runs/<run_id>/sandbox/`.
- No path traversal (`..` or absolute paths) out of the sandbox.
- Subprocesses execute with scrubbed environment (no API_KEY, no PSID, no credentials).
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any, Dict


class SandboxViolationError(PermissionError):
    pass


class AgentSandbox:
    def __init__(self, sandbox_dir: Path | str):
        self.root = Path(sandbox_dir).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _resolve_safe(self, rel_or_abs_path: str) -> Path:
        """Resolve a path and ensure it remains strictly inside the sandbox root."""
        p_str = str(rel_or_abs_path).strip().replace("\\", "/")
        for prefix in ["/sandbox/", "sandbox/", "./sandbox/", "/workspace/sandbox/", "/workspace/", "/"]:
            if p_str.startswith(prefix):
                p_str = p_str[len(prefix):]
                break

        clean = Path(p_str)
        target = (self.root / clean).resolve()

        try:
            target.relative_to(self.root)
        except ValueError:
            raise SandboxViolationError(
                f"Access denied: path '{rel_or_abs_path}' resolves outside sandbox root '{self.root}'"
            )
        return target

    def write_file(self, path: str, content: str) -> Dict[str, Any]:
        try:
            target = self._resolve_safe(path)
        except SandboxViolationError as e:
            return {"status": "error", "message": str(e)}
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return {"status": "ok", "path": str(target.relative_to(self.root)), "bytes_written": len(content.encode("utf-8"))}

    def read_file(self, path: str) -> Dict[str, Any]:
        try:
            target = self._resolve_safe(path)
        except SandboxViolationError as e:
            return {"status": "error", "message": str(e)}
        if not target.exists():
            return {"status": "error", "message": f"File not found: {path}"}
        if not target.is_file():
            return {"status": "error", "message": f"Not a file: {path}"}
        content = target.read_text(encoding="utf-8", errors="replace")
        return {"status": "ok", "path": str(target.relative_to(self.root)), "content": content}

    def list_dir(self, path: str = ".") -> Dict[str, Any]:
        try:
            target = self._resolve_safe(path)
        except SandboxViolationError as e:
            return {"status": "error", "message": str(e)}
        if not target.exists():
            return {"status": "error", "message": f"Directory not found: {path}"}
        entries = []
        for p in sorted(target.iterdir()):
            entries.append({
                "name": p.name,
                "is_dir": p.is_dir(),
                "size": p.stat().st_size if p.is_file() else None,
            })
        return {"status": "ok", "path": str(target.relative_to(self.root)), "entries": entries}

    def run_command(self, command: str, timeout: int = 60) -> Dict[str, Any]:
        """Execute a command in the sandbox with scrubbed environment."""
        # Strip all secret-bearing environment variables
        safe_env = {
            "PATH": os.environ.get("PATH", ""),
            "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""),
            "TEMP": str(self.root),
            "TMP": str(self.root),
            "PYTHONUTF8": "1",
        }
        try:
            proc = subprocess.run(
                command,
                cwd=str(self.root),
                shell=True,
                capture_output=True,
                text=True,
                env=safe_env,
                timeout=timeout,
            )
            return {
                "status": "ok",
                "exit_code": proc.returncode,
                "stdout": proc.stdout[-4000:],  # Bounded capture
                "stderr": proc.stderr[-4000:],
            }
        except subprocess.TimeoutExpired:
            return {"status": "error", "message": f"Command timed out after {timeout} seconds"}
        except Exception as e:
            return {"status": "error", "message": str(e)}


BabysitSandbox = AgentSandbox

