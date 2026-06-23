from __future__ import annotations

import functools
import hashlib
import ipaddress
import re
import socket
import struct
import subprocess
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, unquote, urlparse


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
        directory = self.directory

        class _ChecksumHandler(SimpleHTTPRequestHandler):
            """`SimpleHTTPRequestHandler` plus a `<file>.sha256` endpoint.

            For any path ending in `.sha256`, computes the SHA256 of the
            corresponding file in `directory` on demand. Useful for clients
            that want to verify the download without re-implementing their
            own checksum capture from the wget output.
            """

            def do_GET(self_inner) -> None:  # noqa: N805
                parsed = urlparse(self_inner.path)
                path = unquote(parsed.path)
                if path.endswith(".sha256"):
                    target = directory / Path(path[1:]).name[: -len(".sha256")]
                    if target.is_file():
                        digest = hashlib.sha256()
                        with target.open("rb") as fh:
                            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                                digest.update(chunk)
                        body = f"{digest.hexdigest()}  {target.name}\n".encode()
                        self_inner.send_response(200)
                        self_inner.send_header("Content-Type", "text/plain")
                        self_inner.send_header("Content-Length", str(len(body)))
                        self_inner.end_headers()
                        self_inner.wfile.write(body)
                        return
                    self_inner.send_error(404, f"no such file: {target.name}")
                    return
                super().do_GET()

        handler = functools.partial(
            _ChecksumHandler,
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


class TemporaryTftpFirmwareServer:
    """Small read-only TFTP server for OpenWrt-shell transfers.

    macOS launchd's system tftpd is convenient for U-Boot recovery on UDP/69,
    but OpenWrt's BusyBox client can request `HOST PORT`. Binding a temporary
    high port keeps normal shell transfers self-contained and avoids requiring
    root-owned daemon state for every run.
    """

    _BLOCK_SIZE = 512

    def __init__(
        self,
        *,
        directory: Path,
        bind: str = "0.0.0.0",
        port: int = 0,
        timeout_sec: float = 2.0,
        retries: int = 5,
    ) -> None:
        self.directory = directory
        self.bind = bind
        self.port = port
        self.timeout_sec = timeout_sec
        self.retries = retries
        self._socket: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    @property
    def actual_port(self) -> int:
        if self._socket is None:
            raise FirmwareServerError("firmware TFTP server is not running")
        return int(self._socket.getsockname()[1])

    def start(self) -> None:
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((self.bind, self.port))
            sock.settimeout(0.2)
        except OSError as exc:
            raise FirmwareServerError(f"cannot start firmware TFTP server: {exc}") from exc

        self._socket = sock
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._serve,
            name="owrt-monitor-firmware-tftp",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._socket is not None:
            self._socket.close()
            self._socket = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def _serve(self) -> None:
        sock = self._socket
        if sock is None:
            return
        while not self._stop.is_set():
            try:
                data, client = sock.recvfrom(2048)
            except TimeoutError:
                continue
            except OSError:
                return
            try:
                self._handle_request(sock, data, client)
            except OSError:
                if self._stop.is_set():
                    return

    def _handle_request(
        self,
        sock: socket.socket,
        data: bytes,
        client: tuple[str, int],
    ) -> None:
        if len(data) < 4:
            return
        opcode = struct.unpack("!H", data[:2])[0]
        if opcode != 1:
            self._send_error(sock, client, 4, "only RRQ is supported")
            return

        parts = data[2:].split(b"\0")
        if not parts or not parts[0]:
            self._send_error(sock, client, 4, "missing filename")
            return
        filename = parts[0].decode("utf-8", errors="ignore")
        target = self.directory / Path(filename).name
        if not target.is_file():
            self._send_error(sock, client, 1, f"no such file: {Path(filename).name}")
            return
        self._send_file(sock, client, target)

    def _send_file(
        self,
        sock: socket.socket,
        client: tuple[str, int],
        target: Path,
    ) -> None:
        block = 1
        with target.open("rb") as fh:
            while not self._stop.is_set():
                chunk = fh.read(self._BLOCK_SIZE)
                packet = struct.pack("!HH", 3, block) + chunk
                if not self._send_and_wait_for_ack(sock, client, packet, block):
                    return
                if len(chunk) < self._BLOCK_SIZE:
                    return
                block = (block + 1) & 0xFFFF

    def _send_and_wait_for_ack(
        self,
        sock: socket.socket,
        client: tuple[str, int],
        packet: bytes,
        block: int,
    ) -> bool:
        sock.settimeout(self.timeout_sec)
        try:
            for _ in range(self.retries):
                sock.sendto(packet, client)
                try:
                    data, ack_client = sock.recvfrom(2048)
                except TimeoutError:
                    continue
                if ack_client != client or len(data) < 4:
                    continue
                opcode, ack_block = struct.unpack("!HH", data[:4])
                if opcode == 4 and ack_block == block:
                    return True
                if opcode == 5:
                    return False
            return False
        finally:
            sock.settimeout(0.2)

    def _send_error(
        self,
        sock: socket.socket,
        client: tuple[str, int],
        code: int,
        message: str,
    ) -> None:
        packet = struct.pack("!HH", 5, code) + message.encode("utf-8", errors="replace") + b"\0"
        sock.sendto(packet, client)


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


def infer_host_for_interface(interface_or_service: str | None) -> str | None:
    """Return the current IPv4 for a host interface or macOS network service.

    macOS labs often expose USB Ethernet as a service/hardware-port name like
    `USB 10/100/1000 LAN`, while low-level tools use a BSD device like `en7`.
    Try both shapes before falling back to Linux's `ip` output.
    """
    if interface_or_service is None:
        return None
    name = interface_or_service.strip()
    if not name:
        return None

    direct = _interface_ipv4(name)
    if direct:
        return direct

    for device in _macos_network_service_devices(name):
        ip = _interface_ipv4(device)
        if ip:
            return ip

    for device in _macos_hardware_port_devices(name):
        ip = _interface_ipv4(device)
        if ip:
            return ip

    return None


def _interface_ipv4(device: str) -> str | None:
    return _ifconfig_inet_addr(device) or _linux_ip_addr(device)


def _macos_network_service_devices(service: str) -> list[str]:
    completed = _run_probe(["networksetup", "-listnetworkserviceorder"])
    if completed is None or completed.returncode != 0:
        return []

    devices: list[str] = []
    current_service: str | None = None
    for raw_line in completed.stdout.splitlines():
        line = raw_line.strip()
        service_match = re.match(r"(?:\(\d+\)|\(\*\))\s+(.+)$", line)
        if service_match is not None:
            current_service = service_match.group(1).strip()
            continue
        if current_service != service:
            continue
        device_match = re.search(r",\s*Device:\s*([^)]+)\)", line)
        if device_match is None:
            continue
        device = device_match.group(1).strip()
        if device:
            devices.append(device)
    return devices


def _macos_hardware_port_devices(hardware_port: str) -> list[str]:
    completed = _run_probe(["networksetup", "-listallhardwareports"])
    if completed is None or completed.returncode != 0:
        return []

    devices: list[str] = []
    current_port: str | None = None
    for raw_line in completed.stdout.splitlines():
        line = raw_line.strip()
        if not line:
            current_port = None
            continue
        if line.startswith("Hardware Port:"):
            current_port = line.partition(":")[2].strip()
            continue
        if current_port != hardware_port or not line.startswith("Device:"):
            continue
        device = line.partition(":")[2].strip()
        if device:
            devices.append(device)
    return devices


def _linux_ip_addr(device: str) -> str | None:
    completed = _run_probe(["ip", "-4", "-o", "addr", "show", "dev", device])
    if completed is None or completed.returncode != 0:
        return None
    match = re.search(r"\binet\s+(\d+(?:\.\d+){3})/", completed.stdout)
    if match is None:
        return None
    return _valid_ipv4_or_none(match.group(1))


def _ifconfig_inet_addr(device: str) -> str | None:
    completed = _run_probe(["ifconfig", device])
    if completed is None or completed.returncode != 0:
        return None
    match = re.search(r"^\s*inet\s+(\d+(?:\.\d+){3})\b", completed.stdout, re.MULTILINE)
    if match is None:
        return None
    return _valid_ipv4_or_none(match.group(1))


def _run_probe(command: list[str]) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


def _valid_ipv4_or_none(value: str) -> str | None:
    try:
        parsed = ipaddress.ip_address(value)
    except ValueError:
        return None
    if parsed.version != 4:
        return None
    return str(parsed)
