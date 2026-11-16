"""A fake RomM over real HTTP, for tests that drive the RomM routes end to end.

It answers the endpoints the app reads, in RomM's own shapes, and records every
request so a test can check what RomM was asked. The shapes come from RomM's
source at ``eb10d95``:

* ``GET /api/heartbeat`` (``backend/endpoints/heartbeat.py``): the version under
  ``SYSTEM.VERSION`` and, on a RomM with the converter, ``CONVERTO.ENABLED``
  (``ROM_CONVERTO_ENABLED`` is set and the rom-converto binary answers).
* ``GET /api/config`` (``backend/endpoints/configs.py``): ``CONVERTO`` is the
  ``converto:`` block of ``config.yml`` (``ConvertoConfig``), with the
  ``platform_formats`` map of Platform slug to target, and
  ``CONVERTO_LIBRARY_TARGETS`` maps each slug to the targets a library can be
  stored in (``LIBRARY_TARGETS_BY_PLATFORM`` in
  ``backend/adapters/services/rom_converto.py``).
* ``GET /api/platforms``: a flat list of platforms.

``converter=False`` is a RomM from before the converter: neither ``CONVERTO``
key is served.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

# RomM's LIBRARY_TARGETS_BY_PLATFORM at eb10d95, as /api/config serves it
# (each list sorted). Xbox's `xiso` is lossy, so it is never a library target.
LIBRARY_TARGETS: dict[str, list[str]] = {
    "3ds": ["cci", "cia", "z3ds"],
    "dc": ["chd"],
    "ngc": ["iso", "rvz"],
    "ps2": ["chd", "cso", "iso", "zso"],
    "psp": ["chd", "cso", "iso", "zso"],
    "psx": ["chd"],
    "saturn": ["chd"],
    "segacd": ["chd"],
    "switch": ["nsp", "nsz", "xci", "xcz"],
    "switch-2": ["nsp", "nsz", "xci", "xcz"],
    "wii": ["iso", "rvz", "wbfs"],
    "xbox360": ["zar"],
}


class FakeRomm:
    """One fake RomM instance. Start with :meth:`start`, stop with :meth:`stop`."""

    def __init__(
        self,
        *,
        version: str = "4.9.0",
        converter: bool = True,
        enabled: bool = True,
        platform_formats: dict[str, str] | None = None,
        platforms: list[dict] | None = None,
        config_status: int = 200,
    ) -> None:
        self.version = version
        self.converter = converter
        self.enabled = enabled
        self.platform_formats = dict(platform_formats or {})
        self.platforms = list(platforms or [])
        # Anything but 200 makes /api/config fail with that status.
        self.config_status = config_status
        self.requests: list[tuple[str, str]] = []
        self._server: ThreadingHTTPServer | None = None

    @property
    def url(self) -> str:
        assert self._server is not None, "start() the fake first"
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def paths(self) -> list[str]:
        """The paths requested so far, in order, without query strings."""
        return [path.split("?", 1)[0] for _method, path in self.requests]

    def heartbeat(self) -> dict[str, Any]:
        body: dict[str, Any] = {"SYSTEM": {"VERSION": self.version, "SHOW_SETUP_WIZARD": False}}
        if self.converter:
            body["CONVERTO"] = {"ENABLED": self.enabled}
        return body

    def config(self) -> dict[str, Any]:
        body: dict[str, Any] = {
            "CONFIG_FILE_MOUNTED": True,
            "CONFIG_FILE_WRITABLE": True,
            "EXCLUDED_PLATFORMS": [],
        }
        if self.converter:
            body["CONVERTO"] = {
                "download_conversion_enabled": False,
                "scan_metadata": True,
                "cache_ttl_hours": 24,
                "cache_max_size_gb": 20,
                "platform_formats": dict(self.platform_formats),
            }
            body["CONVERTO_LIBRARY_TARGETS"] = {
                slug: list(targets) for slug, targets in LIBRARY_TARGETS.items()
            }
        return body

    def _answer(self, path: str) -> tuple[int, Any]:
        route = path.split("?", 1)[0]
        if route == "/api/heartbeat":
            return 200, self.heartbeat()
        if route == "/api/config":
            if self.config_status != 200:
                return self.config_status, {"detail": "config unavailable"}
            return 200, self.config()
        if route == "/api/platforms":
            return 200, self.platforms
        return 404, {"detail": "Not Found"}

    def start(self) -> FakeRomm:
        fake = self

        class _Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                fake.requests.append(("GET", self.path))
                status, body = fake._answer(self.path)
                raw = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, *_args) -> None:
                return

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        return self

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
