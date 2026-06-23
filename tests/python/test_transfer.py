import hashlib
import socket
import struct
from http.client import HTTPConnection
from pathlib import Path
from urllib.parse import urlparse

from owrt_monitor.transfer import TemporaryFirmwareServer, TemporaryTftpFirmwareServer


def _http_get(url: str) -> tuple[int, bytes]:
    """Tiny HTTP client for tests.

    Avoids `urllib.request.urlopen` (which semgrep flags because urllib also
    accepts `file://`) — `http.client` is HTTP-only and is the right fit for
    fetching from a localhost test server.
    """
    parsed = urlparse(url)
    conn = HTTPConnection(parsed.hostname, parsed.port, timeout=5)
    try:
        path = parsed.path or "/"
        if parsed.query:
            path = f"{path}?{parsed.query}"
        conn.request("GET", path)
        response = conn.getresponse()
        body = response.read()
        return response.status, body
    finally:
        conn.close()


def test_temporary_firmware_server_serves_directory(tmp_path: Path) -> None:
    firmware = tmp_path / "firmware.bin"
    firmware.write_bytes(b"firmware")
    server = TemporaryFirmwareServer(directory=tmp_path, bind="127.0.0.1", port=0)

    try:
        server.start()
        status, body = _http_get(server.url_for("firmware.bin", host="127.0.0.1"))
        assert status == 200
        assert body == b"firmware"
    finally:
        server.stop()


def test_checksum_endpoint_returns_sha256(tmp_path: Path) -> None:
    firmware = tmp_path / "firmware.bin"
    payload = b"FAKE_FIRMWARE_PAYLOAD" * 1000
    firmware.write_bytes(payload)
    expected = hashlib.sha256(payload).hexdigest()

    server = TemporaryFirmwareServer(directory=tmp_path, bind="127.0.0.1", port=0)
    try:
        server.start()
        status, body = _http_get(server.url_for("firmware.bin.sha256", host="127.0.0.1"))
        assert status == 200
        decoded = body.decode("utf-8").strip()
        assert decoded.startswith(expected)
        assert decoded.endswith("firmware.bin")
    finally:
        server.stop()


def test_checksum_endpoint_returns_404_for_missing_file(tmp_path: Path) -> None:
    server = TemporaryFirmwareServer(directory=tmp_path, bind="127.0.0.1", port=0)
    try:
        server.start()
        status, _ = _http_get(server.url_for("nope.bin.sha256", host="127.0.0.1"))
        assert status == 404
    finally:
        server.stop()


def test_temporary_tftp_firmware_server_serves_file(tmp_path: Path) -> None:
    firmware = tmp_path / "firmware.bin"
    firmware.write_bytes(b"firmware-over-tftp")
    server = TemporaryTftpFirmwareServer(directory=tmp_path, bind="127.0.0.1", port=0)

    try:
        server.start()
        assert _tftp_get("127.0.0.1", server.actual_port, "firmware.bin") == firmware.read_bytes()
    finally:
        server.stop()


def _tftp_get(host: str, port: int, filename: str) -> bytes:
    request = struct.pack("!H", 1) + filename.encode("ascii") + b"\0octet\0"
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(5)
    try:
        sock.sendto(request, (host, port))
        chunks: list[bytes] = []
        expected_block = 1
        server_addr: tuple[str, int] | None = None
        while True:
            data, addr = sock.recvfrom(2048)
            if server_addr is None:
                server_addr = addr
            assert addr == server_addr
            opcode, block = struct.unpack("!HH", data[:4])
            assert opcode == 3
            assert block == expected_block
            chunk = data[4:]
            chunks.append(chunk)
            sock.sendto(struct.pack("!HH", 4, block), addr)
            if len(chunk) < 512:
                return b"".join(chunks)
            expected_block = (expected_block + 1) & 0xFFFF
    finally:
        sock.close()
