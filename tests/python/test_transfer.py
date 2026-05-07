from pathlib import Path
from urllib.request import urlopen

from owrt_monitor.transfer import TemporaryFirmwareServer


def test_temporary_firmware_server_serves_directory(tmp_path: Path) -> None:
    firmware = tmp_path / "firmware.bin"
    firmware.write_bytes(b"firmware")
    server = TemporaryFirmwareServer(directory=tmp_path, bind="127.0.0.1", port=0)

    try:
        server.start()
        with urlopen(server.url_for("firmware.bin", host="127.0.0.1"), timeout=5) as response:
            assert response.read() == b"firmware"
    finally:
        server.stop()
