"""Code-based cron scripts — deterministic Python as cron callbacks.

Scripts under ~/.kiroclaw/crons/ are LLM-writeable by design. The sandbox +
path-restriction prevents filesystem escape, but the LLM can register
self-written scripts. Mitigations: SEL audit trail on every invocation,
is_sensitive_path() blocks credential files, auto-pause after 5 consecutive
failures, concurrent execution guard prevents double-fire.

Usage:
    # ~/.kiroclaw/crons/my_monitor.py
    from kiro_claw.cron_script import Skip, Done

    def run(ctx):
        data = ctx.call_tool("kiroclaw-core", "browse_search", {"query": "..."})
        if not ready(data):
            raise Skip()  # silent, retry next tick
        ctx.notify("Done: " + summary)
        raise Done()  # remove cron job
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import tempfile
import urllib.request
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any

from kiro_claw.sandbox import wrap_argv
from kiro_claw.security import is_sensitive_path, redact_credentials, redact_exfiltration_urls
from kiro_claw.sel import sel

if TYPE_CHECKING:
    from kiro_claw.cron import CronJob

logger = logging.getLogger(__name__)


class Skip(Exception):
    """Abort this tick silently. Cron fires again next interval."""


class Done(Exception):
    """Complete the cron job. Job is removed from the schedule.

    Use ctx.notify() before raising Done() to deliver a message.
    """

    def __init__(self, message: str = ""):
        self.message = message
        super().__init__(message)


class Report(Exception):
    """Deliver a message but keep the job running.

    Use for long-lived monitors that need to report multiple times.
    """

    def __init__(self, message: str = ""):
        self.message = message
        super().__init__(message)


@dataclass
class ScriptContext:
    """Passed to script functions. Provides delivery and tool access."""

    job: CronJob
    _port: int = 7777
    _secret: str = ""

    def __post_init__(self) -> None:
        self._port = int(os.environ.get("KIROCLAW_PORT", "7777"))
        # Secret injected via temp file (not inherited env) to prevent privilege escalation.
        # Pop env var and unlink file immediately so fn(ctx) cannot access the secret directly.
        secret_file = os.environ.pop("_KIROCLAW_SECRET_FILE", "")
        if secret_file and Path(secret_file).exists():
            self._secret = Path(secret_file).read_text()
            try:
                Path(secret_file).unlink()
            except OSError:
                pass
        else:
            self._secret = os.environ.pop("KIROCLAW_INTERNAL_SECRET", "")

    @property
    def message(self) -> str:
        """The cron job's message field (used to pass args to scripts)."""
        return getattr(self.job, "message", "")

    def notify(self, text: str, **kwargs: Any) -> dict:
        """Send a message via the gateway (same as send_message MCP tool).

        Raises RuntimeError if delivery fails.
        """
        safe_text = redact_exfiltration_urls(redact_credentials(text)[0])[0]
        # Redact kwargs values
        kwargs_str = json.dumps(kwargs) if kwargs else "{}"
        kwargs_str = redact_exfiltration_urls(redact_credentials(kwargs_str)[0])[0]
        safe_kwargs = json.loads(kwargs_str) if kwargs else {}
        payload: dict[str, Any] = {"text": safe_text, **safe_kwargs}
        result = self._post("/api/send-message", payload)
        if "error" in result:
            raise RuntimeError(f"notify() failed: {result['error']}")
        return result

    def call_tool(self, server: str, tool: str, args: dict) -> str:
        """Call an MCP tool by spawning the server subprocess directly.

        Args are scanned for credential/URL leakage before passing to the
        sandboxed MCP server subprocess.
        """
        # Scan serialized args for credential patterns
        args_str = json.dumps(args)
        args_str = redact_exfiltration_urls(redact_credentials(args_str)[0])[0]
        safe_args = json.loads(args_str)
        client = None
        try:
            client = McpToolClient(server)
            result = client.call_tool(tool, safe_args)
            self._audit_tool_call(server, tool, "ok")
            return result
        except Exception as exc:
            self._audit_tool_call(server, tool, "error", str(exc))
            raise
        finally:
            if client is not None:
                client.close()

    def _audit_tool_call(self, server: str, tool: str, outcome: str, error: str = "") -> None:
        """Log tool invocation for audit trail."""
        logger.info(
            "cron_script tool_call: job=%s server=%s tool=%s outcome=%s%s",
            self.job.id, server, tool, outcome, f" error={error}" if error else "",
        )
        try:
            sel().log_tool_invocation(
                session_key=f"cron:{self.job.id}",
                tool_name=f"{server}/{tool}",
                tool_kind="cron_script_tool",
                outcome=outcome,
                error=error,
            )
        except Exception:
            logger.debug("SEL audit logging failed in cron_script tool call", exc_info=True)

    def _post(self, path: str, body: dict) -> dict:
        data = json.dumps(body).encode()
        headers = {
            "Content-Type": "application/json",
            "X-Internal-Secret": self._secret,
            "X-Session-Key": f"cron:{self.job.id}",
        }
        req = urllib.request.Request(
            f"http://localhost:{self._port}{path}",
            data=data,
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.loads(resp.read())
        except Exception as exc:
            logger.warning("ScriptContext._post(%s) failed: %s", path, exc)
            return {"error": str(exc)}


# ── MCP Tool Bridge ──


class McpToolClient:
    """Minimal MCP JSON-RPC client. Spawns server subprocess, calls tool, closes."""

    def __init__(self, server_name: str):
        argv = _resolve_mcp_server(server_name)
        if not argv:
            raise RuntimeError(f"MCP server '{server_name}' not found in agent config")
        sandboxed_argv, self._sandbox_cleanup = wrap_argv(list(argv), mode="standard")
        try:
            self._proc = subprocess.Popen(
                sandboxed_argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True
            )
        except Exception:
            if self._sandbox_cleanup:
                Path(self._sandbox_cleanup).unlink(missing_ok=True)
            raise
        assert self._proc.stdin is not None
        assert self._proc.stdout is not None
        self._req_id = 0
        try:
            self._rpc("initialize", {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "kiroclaw-cron-script", "version": "0.1"},
            })
            self._send({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
        except Exception:
            self.close()
            raise

    def _send(self, msg: dict) -> None:
        assert self._proc.stdin is not None
        self._proc.stdin.write(json.dumps(msg) + "\n")
        self._proc.stdin.flush()

    def _recv(self) -> dict | None:
        assert self._proc.stdout is not None
        while True:
            line = self._proc.stdout.readline()
            if not line:  # EOF
                return None
            if line.strip():
                return json.loads(line)

    def _rpc(self, method: str, params: dict | None = None) -> dict:
        self._req_id += 1
        req_id = self._req_id
        self._send({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params or {}})
        for _ in range(1000):
            msg = self._recv()
            if msg is None:
                raise RuntimeError(f"MCP server disconnected during '{method}' call")
            if msg.get("id") == req_id:
                return msg
        raise RuntimeError(f"MCP server did not respond to '{method}' within 1000 messages")

    def call_tool(self, name: str, arguments: dict) -> str:
        r = self._rpc("tools/call", {"name": name, "arguments": arguments})
        if "error" in r:
            raise RuntimeError(f"MCP tool error: {r['error']}")
        result = r.get("result", {})
        if result.get("isError"):
            content = result.get("content", [])
            err_text = content[0].get("text", "unknown error") if content else "unknown error"
            raise RuntimeError(f"MCP tool error: {err_text}")
        content = result.get("content", [])
        return content[0].get("text", "") if content else ""

    def close(self) -> None:
        try:
            self._proc.terminate()
            self._proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self._proc.kill()
            self._proc.wait()
        except Exception:
            pass
        finally:
            if self._sandbox_cleanup:
                Path(self._sandbox_cleanup).unlink(missing_ok=True)


@lru_cache(maxsize=16)
def _resolve_mcp_server(name: str) -> tuple[str, ...] | None:
    """Read MCP server command from agent config (cached per process)."""
    cfg_path = Path.home() / ".kiro" / "agents" / "kiroclaw.json"
    if not cfg_path.exists():
        # Fall back to any kiroclaw-named agent spec under ~/.kiro/agents/
        for p in Path.home().glob(".kiro/agents/*kiroclaw*.json"):
            cfg_path = p
            break
    if not cfg_path.exists():
        return None
    cfg = json.loads(cfg_path.read_text())
    spec = cfg.get("mcpServers", {}).get(name)
    if not spec:
        return None
    return tuple([spec["command"]] + spec.get("args", []))


def resolve_script_path(script_path: str) -> tuple[str, str]:
    """Validate and resolve a script path. Returns (file_path, func_name).

    Scripts must be files under ~/.kiroclaw/crons/.
    Format: "~/.kiroclaw/crons/file.py:function" or "/absolute/path.py:function"
    """
    if ":" not in script_path:
        raise ValueError(
            f"Invalid script path '{script_path}': expected 'path.py:func'"
        )
    module_part, func_name = script_path.rsplit(":", 1)

    file_path = Path(os.path.expanduser(module_part)).resolve()
    if not file_path.exists():
        raise FileNotFoundError(f"Script file not found: {file_path}")
    if is_sensitive_path(str(file_path)):
        raise PermissionError(f"Script path blocked by security policy: {file_path}")
    allowed_dir = (Path.home() / ".kiroclaw" / "crons").resolve()
    if not file_path.is_relative_to(allowed_dir):
        raise PermissionError(
            f"Script must be under {allowed_dir}, got: {file_path}"
        )
    return str(file_path), func_name


def run_script_sandboxed(script_path: str, job_id: str, job_message: str = "", timeout: int = 30) -> dict:
    """Run a cron script in a sandboxed subprocess via wrap_argv().

    Returns: {"status": "ok"|"skip"|"done"|"error", "message": "...", "error": "..."}
    """

    file_path_str, func_name = resolve_script_path(script_path)

    launcher = (
        "import sys, json, os, types\n"
        "from kiro_claw.cron_script import ScriptContext, Skip, Done, Report\n"
        f"sys.path.insert(0, os.path.dirname({file_path_str!r}))\n"
        f"mod = types.ModuleType('_cron_script')\n"
        f"mod.__file__ = {file_path_str!r}\n"
        f"with open({file_path_str!r}) as f:\n"
        f"    exec(compile(f.read(), {file_path_str!r}, 'exec'), mod.__dict__)\n"
        f"fn = getattr(mod, {func_name!r}, None)\n"
        "if fn is None:\n"
        f"    print(json.dumps({{'status': 'error', 'error': 'Function not found'}}))\n"
        "    sys.exit(0)\n"
        f"job = types.SimpleNamespace(id={job_id!r}, message={job_message!r})\n"
        "ctx = ScriptContext(job=job)\n"
        "try:\n"
        "    fn(ctx)\n"
        "    print(json.dumps({'status': 'ok'}))\n"
        "except Skip:\n"
        "    print(json.dumps({'status': 'skip'}))\n"
        "except Done as d:\n"
        "    print(json.dumps({'status': 'done', 'message': d.message}))\n"
        "except Report as r:\n"
        "    print(json.dumps({'status': 'report', 'message': r.message}))\n"
        "except Exception as e:\n"
        "    print(json.dumps({'status': 'error', 'error': str(e)}))\n"
    )

    fd, launcher_path = tempfile.mkstemp(suffix=".py", prefix="kiroclaw_cron_")
    sandbox_cleanup: str | None = None
    # Write secret to temp file for ScriptContext (scrubbed from env)
    secret_fd, secret_path = tempfile.mkstemp(prefix="kiroclaw_secret_")
    try:
        os.write(secret_fd, os.environ.get("KIROCLAW_INTERNAL_SECRET", "").encode())
    finally:
        os.close(secret_fd)
    os.chmod(secret_path, 0o600)
    try:
        try:
            os.write(fd, launcher.encode())
        finally:
            os.close(fd)

        argv = [sys.executable, launcher_path]
        sandboxed_argv, sandbox_cleanup = wrap_argv(argv, mode="standard")

        # Build clean env without KIROCLAW_INTERNAL_SECRET
        clean_env = {k: v for k, v in os.environ.items() if k != "KIROCLAW_INTERNAL_SECRET"}
        clean_env["_KIROCLAW_SECRET_FILE"] = secret_path

        proc = subprocess.run(
            sandboxed_argv, capture_output=True, text=True, timeout=timeout, env=clean_env,
        )

        if proc.returncode != 0 and not proc.stdout.strip():
            error_text = proc.stderr[:500] or f"exit {proc.returncode}"
            error_text = redact_exfiltration_urls(redact_credentials(error_text)[0])[0]
            return {"status": "error", "error": error_text}

        try:
            return json.loads(proc.stdout.strip().split("\n")[-1])
        except (json.JSONDecodeError, IndexError):
            return {"status": "error", "error": f"Bad output: {redact_exfiltration_urls(redact_credentials(proc.stdout[:200])[0])[0]}"}
    except subprocess.TimeoutExpired:
        return {"status": "error", "error": f"Script timed out after {timeout}s"}
    finally:
        Path(launcher_path).unlink(missing_ok=True)
        Path(secret_path).unlink(missing_ok=True)
        if sandbox_cleanup:
            Path(sandbox_cleanup).unlink(missing_ok=True)


_MAX_COMMAND_OUTPUT = 65536  # 64KB cap


def run_command_sandboxed(command: str, timeout: int = 300) -> dict:
    """Run a shell command in a sandboxed subprocess via wrap_argv().

    Returns: {"status": "ok"|"error", "output": "...", "exit_code": N}
    """
    argv = ["sh", "-c", command]
    sandboxed_argv, sandbox_cleanup = wrap_argv(argv, mode="standard")
    clean_env = {k: v for k, v in os.environ.items() if k != "KIROCLAW_INTERNAL_SECRET"}
    try:
        proc = subprocess.run(
            sandboxed_argv, capture_output=True, text=True, timeout=timeout, env=clean_env,
        )
        output = proc.stdout
        if len(output) > _MAX_COMMAND_OUTPUT:
            output = output[:_MAX_COMMAND_OUTPUT] + "\n\n[truncated — output exceeded 64KB]"
        if proc.returncode != 0:
            output = f"⚠️ Exit code {proc.returncode}\n\n{output}"
            if proc.stderr:
                output += f"\n\nstderr:\n{proc.stderr[:1000]}"
        return {
            "status": "ok" if proc.returncode == 0 else "error",
            "output": output,
            "exit_code": proc.returncode,
        }
    except subprocess.TimeoutExpired:
        return {"status": "error", "output": f"❌ Command timed out after {timeout}s", "exit_code": -1}
    except Exception as exc:
        return {"status": "error", "output": f"❌ Command failed: {exc}", "exit_code": -1}
    finally:
        if sandbox_cleanup:
            Path(sandbox_cleanup).unlink(missing_ok=True)
