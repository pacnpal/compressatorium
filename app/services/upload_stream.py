"""One file from a multipart upload, read as it arrives under a byte cap.

FastAPI's ``UploadFile`` spools the whole request body to a temporary file
before the route runs, so a size check in the route comes after a multi-GB
body has already filled the temp space. :func:`file_upload` reads the body
from ``request.stream()`` through python-multipart's streaming parser
instead: nothing is spooled, a declared ``Content-Length`` over the cap is
refused before a byte is read, and a body that keeps coming (chunked, or
lying about its length) is refused the moment it passes the cap.

The cap on the body is *max_bytes* plus :data:`MULTIPART_OVERHEAD` for the
framing (boundaries, part headers, small form fields). The caller still holds
the file itself to *max_bytes*, as it counts what it keeps.
"""

from __future__ import annotations

from collections import deque
from collections.abc import AsyncGenerator
from dataclasses import dataclass

from python_multipart.exceptions import MultipartParseError
from python_multipart.multipart import MultipartParser, parse_options_header
from starlette.requests import Request

# Room for the multipart framing around the file: boundary lines, part
# headers (a long file name), and any small fields sent alongside.
MULTIPART_OVERHEAD = 64 * 1024


class UploadRefused(Exception):
    """The upload can't be read. *status* is the HTTP answer for a route."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass
class FileUpload:
    """The file part: its name as sent, and its bytes as they arrive."""

    filename: str
    chunks: AsyncGenerator[bytes, None]


def _too_large(max_bytes: int) -> UploadRefused:
    return UploadRefused(413, f"The upload is over the size limit ({max_bytes} bytes)")


def _declared_length(request: Request) -> int | None:
    value = request.headers.get("content-length")
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        raise UploadRefused(400, "The Content-Length header isn't a number") from None


def _decode(value: bytes) -> str:
    try:
        return value.decode("utf-8")
    except UnicodeDecodeError:
        return value.decode("latin-1")


async def _events(request: Request, boundary: bytes, body_cap: int | None):
    """The parser's events, in order, as the body arrives: ``("begin",)``,
    ``("header", name, value)``, ``("headers",)``, ``("data", bytes)``,
    ``("end",)``. Raises 413 once more than *body_cap* bytes have come."""
    pending: deque = deque()
    header = {"name": b"", "value": b""}

    def on_header_field(data: bytes, start: int, end: int) -> None:
        header["name"] += data[start:end]

    def on_header_value(data: bytes, start: int, end: int) -> None:
        header["value"] += data[start:end]

    def on_header_end() -> None:
        pending.append(("header", header["name"].lower(), header["value"]))
        header["name"] = header["value"] = b""

    parser = MultipartParser(boundary, {
        "on_part_begin": lambda: pending.append(("begin",)),
        "on_header_field": on_header_field,
        "on_header_value": on_header_value,
        "on_header_end": on_header_end,
        "on_headers_finished": lambda: pending.append(("headers",)),
        "on_part_data": lambda data, start, end: pending.append(("data", data[start:end])),
        "on_part_end": lambda: pending.append(("end",)),
    })
    received = 0
    try:
        async for data in request.stream():
            received += len(data)
            if body_cap is not None and received > body_cap:
                raise _too_large(body_cap - MULTIPART_OVERHEAD)
            parser.write(data)
            while pending:
                yield pending.popleft()
        parser.finalize()
    except MultipartParseError as exc:
        raise UploadRefused(400, f"The upload isn't readable multipart data: {exc}") from None
    while pending:
        yield pending.popleft()


def _open(request: Request, max_bytes: int):
    """The body's parser events under the cap; refuses (413/400) before a
    byte is read when the headers already say no."""
    body_cap = max_bytes + MULTIPART_OVERHEAD if max_bytes > 0 else None
    declared = _declared_length(request)
    if body_cap is not None and declared is not None and declared > body_cap:
        raise _too_large(max_bytes)
    kind, options = parse_options_header(request.headers.get("content-type"))
    boundary = options.get(b"boundary")
    if kind != b"multipart/form-data" or not boundary:
        raise UploadRefused(400, "Send the file as multipart/form-data")
    return _events(request, boundary, body_cap)


async def _next_file(events, field: str) -> str | None:
    """Read on to the next *field* file part's data; its name, or None at the end."""
    disposition = b""
    async for event in events:
        if event[0] == "begin":
            disposition = b""
        elif event[0] == "header" and event[1] == b"content-disposition":
            disposition = event[2]
        elif event[0] == "headers":
            _kind, params = parse_options_header(disposition)
            if params.get(b"name") == field.encode() and b"filename" in params:
                return _decode(params[b"filename"])
    return None


async def _current_part(events) -> AsyncGenerator[bytes, None]:
    """The current part's bytes, up to its end."""
    async for event in events:
        if event[0] == "end":
            return
        if event[0] == "data":
            yield event[1]


async def file_upload(request: Request, *, field: str, max_bytes: int) -> FileUpload:
    """The *field* file part of *request*, read as it arrives.

    *max_bytes* of 0 (or less) sets no cap. Raises :class:`UploadRefused`:
    413 for a body over the cap (by its ``Content-Length``, or as it comes),
    400 for a body that isn't multipart or holds no *field* file. Any other
    part is read past and dropped. Iterate ``chunks`` to the end (or close it)
    to finish reading the body.
    """
    events = _open(request, max_bytes)
    filename = await _next_file(events, field)
    if filename is None:
        await events.aclose()
        raise UploadRefused(400, f"The upload holds no {field!r} file")
    return FileUpload(filename, _part_data(events))


async def file_uploads(
    request: Request, *, field: str, max_bytes: int,
) -> AsyncGenerator[FileUpload, None]:
    """Every *field* file part of *request*, in order, read as it arrives.

    *max_bytes* caps the whole body (all the files together), as in
    :func:`file_upload`, which raises the same :class:`UploadRefused`. Each
    file's ``chunks`` is valid until the next file is asked for; what is left
    unread of it is read past then (still under the cap). Other parts are
    read past and dropped.
    """
    events = _open(request, max_bytes)
    try:
        while (filename := await _next_file(events, field)) is not None:
            part = _current_part(events)
            yield FileUpload(filename, part)
            await part.aclose()
    finally:
        await events.aclose()


async def _part_data(events) -> AsyncGenerator[bytes, None]:
    """The current part's bytes, up to its end; the rest of the body is then
    read through and dropped, so the parser sees it end cleanly."""
    try:
        async for chunk in _current_part(events):
            yield chunk
        async for _event in events:
            pass
    finally:
        await events.aclose()
