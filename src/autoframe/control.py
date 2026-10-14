"""Control socket: one JSON request per connection, one JSON reply.

Requests: {"cmd": "status"} and {"cmd": "reload"}. The socket lives at
$XDG_RUNTIME_DIR/autoframe/control.sock and is only accessible to the user.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import threading
from collections.abc import Callable
from pathlib import Path

from .config import runtime_dir

log = logging.getLogger(__name__)


def socket_path() -> Path:
    return runtime_dir() / "control.sock"


class AlreadyRunning(RuntimeError):
    pass


class ControlServer:
    def __init__(self, handler: Callable[[dict], dict]):
        self.handler = handler
        self.path = socket_path()
        self.sock: socket.socket | None = None

    def start(self) -> ControlServer:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.path.exists():
            try:
                request({"cmd": "status"}, timeout=1.0)
            except OSError:
                self.path.unlink()  # stale socket from a crashed run
            else:
                raise AlreadyRunning(f"autoframe is already running (socket {self.path})")
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        old = os.umask(0o177)
        try:
            self.sock.bind(str(self.path))
        finally:
            os.umask(old)
        self.sock.listen(4)
        threading.Thread(target=self._serve, name="control", daemon=True).start()
        return self

    def _serve(self) -> None:
        while self.sock is not None:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            with conn:
                conn.settimeout(2.0)
                try:
                    req = json.loads(_read_line(conn))
                    reply = self.handler(req)
                except Exception as e:  # report errors to the client, keep serving
                    reply = {"ok": False, "error": str(e)}
                try:
                    conn.sendall(json.dumps(reply).encode() + b"\n")
                except OSError:
                    pass

    def close(self) -> None:
        sock, self.sock = self.sock, None
        if sock is not None:
            sock.close()
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass


def _read_line(conn: socket.socket) -> bytes:
    buf = b""
    while not buf.endswith(b"\n"):
        chunk = conn.recv(4096)
        if not chunk:
            break
        buf += chunk
        if len(buf) > 65536:
            raise ValueError("request too large")
    return buf


def request(req: dict, timeout: float = 3.0) -> dict:
    """Send a request to the running service. Raises OSError if it is not running."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        s.connect(str(socket_path()))
        s.sendall(json.dumps(req).encode() + b"\n")
        return json.loads(_read_line(s))
