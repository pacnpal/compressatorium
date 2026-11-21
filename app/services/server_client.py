"""The CLI's way to a running server on its data directory (#533).

``compressatorium convert`` hands its jobs to a server that is running, over
the same HTTP API the web UI uses: ``POST /api/jobs/batch`` to queue,
``GET /api/jobs/{id}`` to follow, ``DELETE /api/jobs/{id}`` to cancel. The
server is found the way the CLI finds everything else, through the data
directory: a running server holds its worker lock (``services.scheduler_lock``)
and names its host and port in it. ``--server URL`` names one outright.

Plain ``http.client``: no redirects, one timeout per call, nothing new to
install. The auth token, when the server has one, is read from the same
place the server keeps it (``COMPRESSATORIUM_AUTH_TOKEN`` or
``<data dir>/auth_token``); nothing is created.
"""
from __future__ import annotations

import asyncio
import contextlib
import http.client
import json
import socket
from pathlib import Path
from urllib import parse as urlparse

from config import settings

# The port the image and run_dev.sh serve on, for a server that has not
# named its own yet (it does on its first /health request).
DEFAULT_PORT = 8080
_TIMEOUT_S = 30


class ServerUnreachable(Exception):
    """A server holds the data directory but did not answer."""


class ServerRefused(Exception):
    """The server answered a call with an error: ``status`` and its detail."""

    def __init__(self, status: int, detail: str) -> None:
        self.status = status
        super().__init__(detail)


def _stored_token() -> str | None:
    if settings.auth_token:
        return settings.auth_token
    try:
        token = (Path(settings.data_dir) / "auth_token").read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return token or None


class Server:
    """One running server, by base URL."""

    def __init__(self, url: str) -> None:
        parts = urlparse.urlsplit(url if "://" in url else f"http://{url}")
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError(f"not an http(s) URL: {url}")
        self.url = f"{parts.scheme}://{parts.netloc}{parts.path.rstrip('/')}"
        self._parts = parts
        self._token = _stored_token()

    @classmethod
    def of_holder(cls, holder: dict) -> Server:
        """The server named in the worker lock's holder note."""
        host = holder.get("host")
        if not host or host == socket.gethostname():
            host = "127.0.0.1"
        return cls(f"http://{host}:{holder.get('port') or DEFAULT_PORT}")

    def _call(self, method: str, path: str, body: dict | None = None):
        parts = self._parts
        factory = (
            http.client.HTTPSConnection if parts.scheme == "https"
            else http.client.HTTPConnection
        )
        default_port = 443 if parts.scheme == "https" else 80
        conn = factory(parts.hostname, parts.port or default_port, timeout=_TIMEOUT_S)
        headers = {"Content-Type": "application/json", "User-Agent": "compressatorium-cli"}
        if self._token:
            headers["X-Compressatorium-Token"] = self._token
        try:
            conn.request(
                method, parts.path.rstrip("/") + path,
                body=None if body is None else json.dumps(body).encode(), headers=headers,
            )
            response = conn.getresponse()
            raw = response.read()
        except (OSError, http.client.HTTPException) as exc:
            reason = getattr(exc, "strerror", None) or type(exc).__name__
            raise ServerUnreachable(f"the server at {self.url} did not answer ({reason})") from None
        finally:
            conn.close()
        try:
            data = json.loads(raw or b"null")
        except ValueError:
            data = None
        if response.status >= 400:
            detail = data.get("detail") if isinstance(data, dict) else None
            raise ServerRefused(response.status, str(detail or f"HTTP {response.status}"))
        return data

    async def call(self, method: str, path: str, body: dict | None = None):
        return await asyncio.to_thread(self._call, method, path, body)

    async def check(self) -> None:
        """Raise :class:`ServerUnreachable` unless the server answers."""
        try:
            await self.call("GET", "/health")
        except ServerRefused as exc:
            raise ServerUnreachable(f"the server at {self.url} answered {exc}") from None

    async def ready_tool_ids(self) -> list[str]:
        """The tools that can run on the server (``GET /api/tools``), which
        decide a run it works: its keys and binaries, not the CLI's."""
        try:
            answer = await self.call("GET", "/api/tools")
        except ServerRefused as exc:
            raise ServerUnreachable(f"the server at {self.url} answered {exc}") from None
        available = answer.get("available") if isinstance(answer, dict) else None
        if not isinstance(available, list):
            raise ServerUnreachable(f"the server at {self.url} did not list its tools")
        return [str(tool_id) for tool_id in available]

    async def queue(self, request: dict) -> tuple[list[dict], list[dict]]:
        """Queue a batch: the jobs it made and the files it left out."""
        answer = await self.call("POST", "/api/jobs/batch?report_skipped=true", request)
        return answer.get("jobs", []), answer.get("skipped", [])

    async def job(self, job_id: str) -> dict | None:
        try:
            return await self.call("GET", f"/api/jobs/{urlparse.quote(job_id)}")
        except ServerRefused as exc:
            if exc.status == 404:
                return None
            raise

    async def cancel(self, job_id: str) -> None:
        with contextlib.suppress(ServerRefused):  # already ended, or gone
            await self.call("DELETE", f"/api/jobs/{urlparse.quote(job_id)}")
