"""Minimal stdio MCP client.

FixPilot is not only an MCP *server*: it can also consume other people's tool
servers as evidence sources (a container runtime, a database inspector, a
design-token server…).  This client is deliberately small — spawn, handshake,
call tools, shut down — and speaks the same JSON-RPC dialect as the server in
:mod:`fixpilot.mcp.server`.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from typing import Any

from .server import PROTOCOL_VERSION


class MCPError(RuntimeError):
    """Raised when an MCP server answers with an error object."""


class MCPToolError(MCPError):
    """The tool ran but reported failure (``isError: true``)."""


class MCPClient:
    def __init__(
        self,
        command: list[str],
        *,
        cwd: str | os.PathLike | None = None,
        env: dict[str, str] | None = None,
        timeout: float = 60.0,
        client_name: str = "fixpilot",
    ) -> None:
        self.command = command
        self.cwd = cwd
        self.env = env
        self.timeout = timeout
        self.client_name = client_name
        self._proc: subprocess.Popen[str] | None = None
        self._next_id = 1
        self._lock = threading.Lock()
        self.server_info: dict = {}

    # -- lifecycle -----------------------------------------------------
    def start(self) -> "MCPClient":
        if self._proc is not None:
            return self
        environment = dict(os.environ)
        if self.env:
            environment.update(self.env)
        self._proc = subprocess.Popen(
            self.command,
            cwd=self.cwd,
            env=environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        init = self.call(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "clientInfo": {"name": self.client_name, "version": "0.4.0"},
            },
        )
        self.server_info = init.get("serverInfo", {})
        self.notify("notifications/initialized", {})
        return self

    def close(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            if proc.stdin:
                proc.stdin.close()
            proc.wait(timeout=3)
        except (OSError, subprocess.TimeoutExpired):
            proc.kill()
        finally:
            # Popen does not close its pipes for us; leaving stdout open leaks a
            # file descriptor per client and warns at interpreter shutdown.
            if proc.stdout:
                proc.stdout.close()

    def __enter__(self) -> "MCPClient":
        return self.start()

    def __exit__(self, *exc_info) -> None:
        self.close()

    # -- protocol ------------------------------------------------------
    def notify(self, method: str, params: dict | None = None) -> None:
        self._write({"jsonrpc": "2.0", "method": method, "params": params or {}})

    def call(self, method: str, params: dict | None = None) -> Any:
        with self._lock:
            request_id = self._next_id
            self._next_id += 1
            self._write({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}})
            deadline = time.monotonic() + self.timeout
            while True:
                message = self._read(deadline)
                if message.get("id") != request_id:
                    continue  # a server notification or a stale reply
                if "error" in message:
                    error = message["error"] or {}
                    raise MCPError(f"{error.get('code')}: {error.get('message')}")
                return message.get("result")

    def list_tools(self) -> list[dict]:
        return list((self.call("tools/list") or {}).get("tools", []))

    def call_tool(self, name: str, arguments: dict | None = None) -> str:
        payload = self.call("tools/call", {"name": name, "arguments": arguments or {}}) or {}
        text = "\n".join(part.get("text", "") for part in payload.get("content", []) if part.get("type") == "text")
        if payload.get("isError"):
            raise MCPToolError(text or f"tool {name} failed")
        return text

    # -- plumbing ------------------------------------------------------
    def _write(self, message: dict) -> None:
        if self._proc is None or self._proc.stdin is None:
            raise MCPError("client is not started")
        self._proc.stdin.write(json.dumps(message) + "\n")
        self._proc.stdin.flush()

    def _read(self, deadline: float) -> dict:
        if self._proc is None or self._proc.stdout is None:
            raise MCPError("client is not started")
        while time.monotonic() < deadline:
            line = self._proc.stdout.readline()
            if not line:
                raise MCPError(f"MCP server closed the connection: {self.command!r} (does the command exist and stay alive?)")
            line = line.strip()
            if not line:
                continue
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue  # servers occasionally log to stdout; ignore non-JSON lines
        raise MCPError(f"timed out waiting for a reply after {self.timeout}s")


__all__ = ["MCPClient", "MCPError", "MCPToolError"]
