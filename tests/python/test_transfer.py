import hashlib
from http.client import HTTPConnection
from pathlib import Path
from urllib.parse import urlparse

from owrt_monitor.transfer import TemporaryFirmwareServer


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
