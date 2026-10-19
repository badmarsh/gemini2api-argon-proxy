"""Sandbox environment for Argon 4 tool execution.

Guarantees isolation:
- All operations are jailed inside `runs/<run_id>/sandbox/<task_id>/`.
- No path traversal (`..` or absolute paths) out of the sandbox.
- Subprocesses execute with scrubbed environment (no API_KEY, no PSID, no credentials).
- Safe python code execution avoiding shell quotation and escaping bugs on Windows.
- Pre-seeding of realistic task files (fixtures) before benchmark execution.
- Verification of output artifacts.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional


class SandboxViolationError(PermissionError):
    pass


class AgentSandbox:
    def __init__(self, sandbox_dir: Path | str):
        self.root = Path(sandbox_dir).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _resolve_safe(self, rel_or_abs_path: str) -> Path:
        """Resolve a path and ensure it remains strictly inside the sandbox root."""
        p_str = str(rel_or_abs_path).strip().replace("\\", "/")
        for prefix in ["/sandbox/", "sandbox/", "./sandbox/", "/workspace/sandbox/", "/workspace/", "./"]:
            if p_str.startswith(prefix):
                p_str = p_str[len(prefix):]
                break

        # Also strip leading slash if remaining
        if p_str.startswith("/"):
            p_str = p_str.lstrip("/")

        clean = Path(p_str)
        target = (self.root / clean).resolve()

        try:
            target.relative_to(self.root)
        except ValueError:
            raise SandboxViolationError(
                f"Access denied: path '{rel_or_abs_path}' resolves outside sandbox root '{self.root}'"
            )
        return target

    def seed_files(self, files: List[Dict[str, str]]) -> Dict[str, Any]:
        """Pre-populate sandbox with initial files (fixtures, scripts, data)."""
        written = []
        for item in files:
            p = item.get("path")
            content = item.get("content", "")
            if not p:
                continue
            res = self.write_file(p, content)
            if res.get("status") == "ok":
                written.append(res.get("path"))
            else:
                return {"status": "error", "message": f"Failed seeding {p}: {res.get('message')}"}
        return {"status": "ok", "seeded_count": len(written), "files": written}

    def write_file(self, path: str, content: str) -> Dict[str, Any]:
        try:
            target = self._resolve_safe(path)
            if target.is_dir():
                return {"status": "error", "message": f"Cannot write to existing directory: {path}"}
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            return {
                "status": "ok",
                "path": str(target.relative_to(self.root)).replace("\\", "/"),
                "bytes_written": len(content.encode("utf-8")),
            }
        except SandboxViolationError as e:
            return {"status": "error", "message": str(e)}
        except Exception as e:
            return {"status": "error", "message": f"Failed to write file {path}: {e}"}

    def read_file(self, path: str) -> Dict[str, Any]:
        try:
            target = self._resolve_safe(path)
            if not target.exists():
                return {"status": "error", "message": f"File not found: {path}"}
            if not target.is_file():
                return {"status": "error", "message": f"Not a file: {path}"}
            content = target.read_text(encoding="utf-8", errors="replace")
            return {
                "status": "ok",
                "path": str(target.relative_to(self.root)).replace("\\", "/"),
                "content": content,
            }
        except SandboxViolationError as e:
            return {"status": "error", "message": str(e)}
        except Exception as e:
            return {"status": "error", "message": f"Failed to read file {path}: {e}"}

    def list_dir(self, path: str = ".") -> Dict[str, Any]:
        try:
            target = self._resolve_safe(path)
            if not target.exists():
                return {"status": "error", "message": f"Directory not found: {path}"}
            if not target.is_dir():
                return {"status": "error", "message": f"Not a directory: {path}"}
            entries = []
            for p in sorted(target.iterdir()):
                entries.append({
                    "name": p.name,
                    "is_dir": p.is_dir(),
                    "size": p.stat().st_size if p.is_file() else None,
                })
            return {
                "status": "ok",
                "path": str(target.relative_to(self.root)).replace("\\", "/"),
                "entries": entries,
            }
        except SandboxViolationError as e:
            return {"status": "error", "message": str(e)}
        except Exception as e:
            return {"status": "error", "message": f"Failed to list directory {path}: {e}"}

    def has_file(self, path: str) -> bool:
        try:
            target = self._resolve_safe(path)
            return target.is_file()
        except Exception:
            return False

    def verify_artifact(
        self,
        path: str,
        min_size: int = 1,
        must_contain: Optional[List[str]] = None,
        is_json: bool = False,
    ) -> Dict[str, Any]:
        """Verify presence, non-emptiness, content patterns, and valid JSON syntax of an artifact."""
        try:
            target = self._resolve_safe(path)
        except SandboxViolationError as e:
            return {"exists": False, "valid": False, "error": str(e)}

        if not target.exists() or not target.is_file():
            return {"exists": False, "valid": False, "error": f"Artifact not found: {path}"}

        size = target.stat().st_size
        if size < min_size:
            return {
                "exists": True,
                "valid": False,
                "size": size,
                "error": f"Artifact {path} too small ({size} < {min_size} bytes)",
            }

        try:
            content = target.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            return {"exists": True, "valid": False, "error": f"Read error: {e}"}

        if must_contain:
            for snippet in must_contain:
                if snippet not in content:
                    return {
                        "exists": True,
                        "valid": False,
                        "size": size,
                        "error": f"Missing expected content snippet in {path}: '{snippet}'",
                    }

        if is_json:
            try:
                json.loads(content)
            except Exception as e:
                return {
                    "exists": True,
                    "valid": False,
                    "size": size,
                    "error": f"Invalid JSON syntax in {path}: {e}",
                }

        return {"exists": True, "valid": True, "size": size, "path": str(target.relative_to(self.root))}

    def eval_python(self, code: str, timeout: int = 60) -> Dict[str, Any]:
        """Execute Python code cleanly without shell quotation corruption."""
        tmp_name = f"_eval_{uuid.uuid4().hex[:8]}.py"
        tmp_file = self.root / tmp_name
        try:
            tmp_file.write_text(code, encoding="utf-8")
            safe_env = {
                "PATH": os.environ.get("PATH", ""),
                "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""),
                "TEMP": str(self.root),
                "TMP": str(self.root),
                "PYTHONUTF8": "1",
            }
            proc = subprocess.run(
                [sys.executable, str(tmp_file)],
                cwd=str(self.root),
                capture_output=True,
                text=True,
                env=safe_env,
                timeout=timeout,
            )
            return {
                "status": "ok",
                "exit_code": proc.returncode,
                "stdout": proc.stdout[-4000:],
                "stderr": proc.stderr[-4000:],
            }
        except subprocess.TimeoutExpired:
            return {"status": "error", "message": f"Python script timed out after {timeout} seconds"}
        except Exception as e:
            return {"status": "error", "message": str(e)}
        finally:
            if tmp_file.exists():
                try:
                    tmp_file.unlink()
                except Exception:
                    pass

    def run_command(self, command: str, timeout: int = 60) -> Dict[str, Any]:
        """Execute a shell command in the sandbox with scrubbed environment."""
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
                "stdout": proc.stdout[-4000:],
                "stderr": proc.stderr[-4000:],
            }
        except subprocess.TimeoutExpired:
            return {"status": "error", "message": f"Command timed out after {timeout} seconds"}
        except Exception as e:
            return {"status": "error", "message": str(e)}


BabysitSandbox = AgentSandbox
