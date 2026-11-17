"""A fake RomM over real HTTP, for tests that drive the RomM routes end to end.

It answers the endpoints the app reads, in RomM's own shapes, and records every
request so a test can check what RomM was asked. The shapes come from RomM's
source at ``eb10d95``:

* ``GET /api/heartbeat`` (``backend/endpoints/heartbeat.py``): the version under
  ``SYSTEM.VERSION`` and, on a RomM with the converter, ``CONVERTO.ENABLED``
  (``ROM_CONVERTO_ENABLED`` is set and the rom-converto binary answers).
  ``title_id_extraction`` serves ``FILESYSTEM.TITLE_ID_EXTRACTION_ENABLED``
  (RomM 5.4.1 sends it; None leaves it out, as an older RomM does), and
  ``heartbeat_status`` makes the heartbeat fail with that status (#393).
* ``GET /api/config`` (``backend/endpoints/configs.py``): ``CONVERTO`` is the
  ``converto:`` block of ``config.yml`` (``ConvertoConfig``), with the
  ``platform_formats`` map of Platform slug to target, and
  ``CONVERTO_LIBRARY_TARGETS`` maps each slug to the targets a library can be
  stored in (``LIBRARY_TARGETS_BY_PLATFORM`` in
  ``backend/adapters/services/rom_converto.py``).
* ``GET /api/platforms``: a flat list of platforms.
* The token-permission probes (#390). RomM has no endpoint that lists a token's
  scopes, so the app asks harmless questions whose answer depends on them (RomM
  5.4.1, spike #382): ``PUT /api/roms/2147483647`` is 404 with ``roms.write``
  and 403 without it, ``GET /api/tasks/status`` is 200 with ``tasks.run`` and
  403 without it, and ``GET /api/roms`` is 200 with ``roms.read``. ``scopes`` is
  the set the fake's token holds; ``probe_status`` makes every probe fail with
  that status instead (a RomM that is down, or rejects the token).

* The swap's calls (#391), on the ROMs the fake holds (:meth:`add_rom`),
  whose files live under ``library`` (RomM's library root as mounted here):

  - ``GET /api/roms?platform_ids=N`` lists a Platform's ROMs a page at a time
    (``limit``, ``offset``; ``total`` counts every match), and
    ``GET /api/roms/{id}`` reads one back.
  - ``PUT /api/roms/{id}`` with a multipart ``fs_name`` renames the record and
    moves its file on disk, as RomM 5.4.1 does (spike #382): a name with a
    path in it is sanitized to its last part, the name the record already has
    is a 200 no-op, a name a file already has is a 409 (``Can't rename: X
    already exists``), and a record whose file is gone is a 500 (``Failed to
    rename ROM N on disk``). A name that would still land outside the ROM's
    own folder (``..``) is a 400: RomM never moves a file out of its folder.
    ``rename_reply`` makes it answer with that status
    and detail instead, changing nothing; ``rename_as`` makes the next rename
    write that name instead of the one asked for (RomM sanitizing a name).
  - ``POST /api/tasks/scan`` with ``{"type": "hashes", "roms_ids": [...]}``
    answers 202 with a ``task_id`` and rehashes those ROMs' files (size and
    SHA-1); ``GET /api/tasks/{task_id}`` reads the task back, ``finished``.
    ``scan_updates=False`` makes the scan finish without rehashing (its stats
    say ``updated_roms: 0`` either way, as RomM's do); ``task_status`` ends it
    with another status.

  ``on_request`` is called with the method and path before each answer, so a
  test can look at the app's state at the moment RomM is asked.

* The re-pin settle pass's calls (#393): ``GET /api/roms/by-hash?sha1_hash=``
  answers the ROM whose SHA-1 that is (404 when none), and a
  ``PUT /api/roms/{id}`` carrying provider ids (``igdb_id`` and the rest, no
  ``fs_name``) writes them and is recorded in ``metadata_writes``.

``converter=False`` is a RomM from before the converter: neither ``CONVERTO``
key is served.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import urllib.parse
import zipfile
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
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


# What a token for the swap holds (an admin user's).
DEFAULT_SCOPES = frozenset({"roms.read", "roms.write", "tasks.run"})


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
        scopes: set[str] | None = None,
        probe_status: int | None = None,
        library: str | os.PathLike | None = None,
        title_id_extraction: bool | None = None,
        heartbeat_status: int = 200,
    ) -> None:
        self.version = version
        self.title_id_extraction = title_id_extraction
        self.heartbeat_status = heartbeat_status
        self.converter = converter
        self.enabled = enabled
        self.platform_formats = dict(platform_formats or {})
        self.platforms = list(platforms or [])
        # Anything but 200 makes /api/config fail with that status.
        self.config_status = config_status
        # The scopes the fake's token holds, as RomM spells them.
        self.scopes = set(DEFAULT_SCOPES if scopes is None else scopes)
        self.probe_status = probe_status
        self.requests: list[tuple[str, str]] = []
        self._server: ThreadingHTTPServer | None = None
        # The swap's side (#391): RomM's library root and the ROMs it holds.
        self.library = Path(library) if library is not None else None
        self.roms: dict[int, dict[str, Any]] = {}
        self.rename_reply: tuple[int, str] | None = None
        self.rename_as: str | None = None
        self.scan_updates = True
        self.task_status = "finished"
        self.on_request: Callable[[str, str], None] | None = None
        # What the app asked: each rename's (ROM id, name) and each scan's body.
        self.renames: list[tuple[int, str]] = []
        # Each metadata write's (ROM id, {field: value}) (#393).
        self.metadata_writes: list[tuple[int, dict[str, str]]] = []
        self.scans: list[dict[str, Any]] = []
        self._tasks: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    @property
    def url(self) -> str:
        assert self._server is not None, "start() the fake first"
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def paths(self) -> list[str]:
        """The paths requested so far, in order, without query strings."""
        return [path.split("?", 1)[0] for _method, path in self.requests]

    @staticmethod
    def scan_platform(folder: str | os.PathLike) -> list[str]:
        """What a RomM 5.4.1 library scan of one Platform folder would index.

        The rule the swap spike observed (#382, ``docs/ROMM_INTEGRATION_ROADMAP.md``,
        "3.0 spike findings"): every file is a ROM, dot-prefixed files included
        (``.Probe PS2.chd.stage`` became a ROM row), and so is every folder (a
        multi-file ROM) except a dot-prefixed one, which RomM skips. Excluded
        extensions such as ``.tmp`` come from the user's ``config.yml``, so
        none are excluded here: the worst case. Sorted names.
        """
        indexed = []
        for entry in os.scandir(folder):
            if entry.is_dir(follow_symlinks=False) and entry.name.startswith("."):
                continue
            indexed.append(entry.name)
        return sorted(indexed)

    # ---- ROMs (#391) ----------------------------------------------------------

    def add_rom(
        self, rom_id: int, platform: dict, fs_name: str, **fields: Any,
    ) -> dict[str, Any]:
        """Hold a ROM for the file ``<library>/<platform fs_slug>/<fs_name>``,
        sized and hashed from disk, in RomM 5.4.1's shape."""
        slug = platform.get("fs_slug") or platform["slug"]
        rom: dict[str, Any] = {
            "id": rom_id,
            "name": os.path.splitext(fs_name)[0],
            "platform_id": platform["id"],
            "platform_slug": platform["slug"],
            "platform_fs_slug": slug,
            "fs_path": f"roms/{slug}",
            "has_multiple_files": False,
            "missing_from_fs": False,
            **fields,
        }
        self.roms[rom_id] = rom
        self._set_name(rom, fs_name)
        self._rehash(rom)
        return rom

    def file_of(self, rom_id: int) -> Path:
        """The file a ROM's record names, on disk."""
        assert self.library is not None, "FakeRomm(library=...) holds no files"
        rom = self.roms[rom_id]
        folder = rom["fs_path"]
        if folder.startswith("roms/") and not (self.library / "roms").is_dir():
            folder = folder[len("roms/"):]
        return self.library / folder / rom["fs_name"]

    @staticmethod
    def _set_name(rom: dict, fs_name: str) -> None:
        rom["fs_name"] = fs_name
        rom["fs_name_no_ext"], rom["fs_extension"] = os.path.splitext(fs_name)
        rom["full_path"] = f"{rom['fs_path']}/{fs_name}"

    def _rehash(self, rom: dict) -> None:
        """Size and SHA-1 as RomM stores them. RomM 5.4.1 hashes a zip by its
        decompressed contents, not the container (Codex on PR #442), so a zip
        gets the SHA-1 of its largest member, which no hash of the zip file
        itself can find."""
        path = self.file_of(rom["id"])
        data = path.read_bytes()
        rom["fs_size_bytes"] = len(data)
        if path.suffix.lower() == ".zip":
            with zipfile.ZipFile(path) as archive:
                largest = max(archive.infolist(), key=lambda info: info.file_size)
                data = archive.read(largest)
        rom["sha1_hash"] = hashlib.sha1(data).hexdigest()

    def _rename(self, rom_id: int, asked: str) -> tuple[int, Any]:
        """``PUT /api/roms/{id}`` with ``fs_name``, as RomM 5.4.1 answers it."""
        self.renames.append((rom_id, asked))
        rom = self.roms.get(rom_id)
        if rom is None:
            return 404, {"detail": f"Rom with id {rom_id} not found"}
        if self.rename_reply is not None:
            status, detail = self.rename_reply
            return status, {"detail": detail}
        # ``rename_as`` is one-shot: the rename that puts the old name back
        # gets the name it asks for.
        name = self.rename_as or asked.replace("\\", "/").rsplit("/", 1)[-1]
        self.rename_as = None
        if name == rom["fs_name"]:
            return 200, dict(rom)
        old = self.file_of(rom_id)
        # RomM never moves a file out of its folder: the new name must
        # resolve to an entry directly in the ROM's own folder.
        folder = os.path.realpath(old.parent)
        new = os.path.realpath(os.path.join(folder, name))
        if not new.startswith(folder + os.sep) or os.path.dirname(new) != folder:
            return 400, {"detail": f"Invalid file name: {asked}"}
        if os.path.lexists(new):
            return 409, {"detail": f"Can't rename: {name} already exists"}
        if not old.is_file():
            return 500, {"detail": f"Failed to rename ROM {rom_id} on disk"}
        os.rename(old, new)
        self._set_name(rom, name)
        return 200, dict(rom)

    def _scan(self, body: dict) -> tuple[int, Any]:
        self.scans.append(body)
        task_id = f"task-{len(self.scans)}"
        if body.get("type") == "hashes" and self.scan_updates:
            for rom_id in body.get("roms_ids") or []:
                if rom_id in self.roms:
                    self._rehash(self.roms[rom_id])
        self._tasks[task_id] = {
            "id": task_id,
            "status": self.task_status,
            # RomM's own stats say nothing changed even when the scan rehashed.
            "result": {"scan_stats": {"updated_roms": 0}},
        }
        return 202, {"task_id": task_id, "status": "queued"}

    def heartbeat(self) -> dict[str, Any]:
        body: dict[str, Any] = {"SYSTEM": {"VERSION": self.version, "SHOW_SETUP_WIZARD": False}}
        if self.title_id_extraction is not None:
            body["FILESYSTEM"] = {"TITLE_ID_EXTRACTION_ENABLED": self.title_id_extraction}
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

    def _gated(self, scope: str, ok: int) -> tuple[int, Any]:
        """*ok* when the token holds *scope*, else RomM's 403."""
        if self.probe_status is not None:
            return self.probe_status, {"detail": "probe failed"}
        if scope in self.scopes:
            return ok, {}
        return 403, {"detail": f"Insufficient scope: {scope}"}

    def _answer(self, method: str, path: str, body: bytes = b"") -> tuple[int, Any]:
        with self._lock:
            return self._answer_locked(method, path, body)

    def _answer_locked(self, method: str, path: str, body: bytes) -> tuple[int, Any]:
        route, _, query = path.partition("?")
        if method == "PUT" and route == "/api/roms/2147483647":
            # The ROM does not exist, so a token allowed to write gets a 404.
            return self._gated("roms.write", 404)
        if method == "GET" and route == "/api/tasks/status":
            return self._gated("tasks.run", 200)
        if method == "GET" and route == "/api/roms":
            status, reply = self._gated("roms.read", 200)
            if status != 200:
                return status, reply
            params = urllib.parse.parse_qs(query)
            wanted = params.get("platform_ids")
            items = [
                dict(rom) for rom in self.roms.values()
                if not wanted or str(rom["platform_id"]) in wanted
            ]
            # One page, as RomM pages it: `total` counts every match.
            offset = int(params.get("offset", ["0"])[0])
            limit = int(params.get("limit", [str(len(items))])[0])
            return 200, {"items": items[offset:offset + limit], "total": len(items)}
        if method == "GET" and route == "/api/roms/by-hash":
            wanted = (urllib.parse.parse_qs(query).get("sha1_hash") or [""])[0].lower()
            found = next(
                (rom for rom in self.roms.values() if rom.get("sha1_hash") == wanted), None,
            )
            return (200, dict(found)) if found else (404, {"detail": "Rom not found"})
        rom_route = re.fullmatch(r"/api/roms/(\d+)", route)
        if rom_route and method == "GET":
            rom = self.roms.get(int(rom_route.group(1)))
            return (200, dict(rom)) if rom else (404, {"detail": "Rom not found"})
        if rom_route and method == "PUT":
            field = re.search(rb'name="fs_name"\r\n\r\n(.*?)\r\n', body, re.DOTALL)
            if field is None:
                rom = self.roms.get(int(rom_route.group(1)))
                if rom is None:
                    return 404, {"detail": "Rom not found"}
                ids = {
                    name.decode(): value.decode() for name, value in re.findall(
                        rb'name="(\w+_id)"\r\n\r\n(.*?)\r\n', body, re.DOTALL,
                    )
                }
                if ids:
                    self.metadata_writes.append((rom["id"], ids))
                    # RomM stores every provider id as an integer except
                    # Flashpoint's, which is text.
                    rom.update({
                        name: value if name == "flashpoint_id" else int(value)
                        for name, value in ids.items()
                    })
                return 200, dict(rom)
            return self._rename(int(rom_route.group(1)), field.group(1).decode())
        if method == "POST" and route == "/api/tasks/scan":
            return self._scan(json.loads(body or b"{}"))
        task_route = re.fullmatch(r"/api/tasks/([\w-]+)", route)
        if task_route and method == "GET" and task_route.group(1) in self._tasks:
            return 200, dict(self._tasks[task_route.group(1)])
        if route == "/api/heartbeat":
            if self.heartbeat_status != 200:
                return self.heartbeat_status, {"detail": "heartbeat unavailable"}
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
                self._serve("GET")

            def do_PUT(self) -> None:
                self._serve("PUT")

            def do_POST(self) -> None:
                self._serve("POST")

            def _serve(self, method: str) -> None:
                # Read the body; draining it also keeps the connection well formed.
                sent = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                fake.requests.append((method, self.path))
                if fake.on_request is not None:
                    fake.on_request(method, self.path)
                status, body = fake._answer(method, self.path, sent)
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
