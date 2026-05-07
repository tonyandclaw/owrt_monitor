from __future__ import annotations

import functools
import socket
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote


class FirmwareServerError(RuntimeError):
    """Raised when the temporary firmware server cannot start."""


class TemporaryFirmwareServer:
    def __init__(self, *, directory: Path, bind: str = "0.0.0.0", port: int = 0) -> None:
        self.directory = directory
        self.bind = bind
        self.port = port
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def actual_port(self) -> int:
        if self._server is None:
            raise FirmwareServerError("firmware server is not running")
        return int(self._server.server_address[1])

    def start(self) -> None:
        handler = functools.partial(
            SimpleHTTPRequestHandler,
            directory=str(self.directory),
        )
        try:
            self._server = ThreadingHTTPServer((self.bind, self.port), handler)
        except OSError as exc:
            raise FirmwareServerError(f"cannot start firmware HTTP server: {exc}") from exc

        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="owrt-monitor-firmware-http",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def url_for(self, filename: str, *, host: str) -> str:
        return f"http://{host}:{self.actual_port}/{quote(filename)}"


def infer_host_for_target(target: str | None) -> str | None:
    if not target:
        return None
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect((target, 9))
        return str(sock.getsockname()[0])
    except OSError:
        return None
    finally:
        sock.close()
