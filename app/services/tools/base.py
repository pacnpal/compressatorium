"""The plugin contract (``ToolPlugin``) and a minimal shared base.

Phase 0 keeps ``BaseTool`` deliberately small: it only hoists the
mode-lookup and derived-extension helpers that every tool needs. The shared
``SubprocessRunner`` orchestration and the ``verify()``-wraps-``verify_stream()``
default described in the design land in a later phase, once real logic is moved
out of the existing service singletons.
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Sequence
from typing import Protocol, runtime_checkable

from pydantic import BaseModel

from .spec import ModeSpec


@runtime_checkable
class ToolPlugin(Protocol):
    id: str
    display_name: str
    binary_path: str
    modes: Sequence[ModeSpec]
    input_extensions: frozenset[str]    # convertible-from
    output_extensions: frozenset[str]   # produced (for "output exists" badges)
    verify_extensions: frozenset[str]   # accepted by verify()

    def spec(self, mode: str) -> ModeSpec: ...

    def output_path(
        self,
        mode: str,
        input_path: str,
        output_dir: str | None = None,
        *,
        treat_as_stem: bool = False,
    ) -> str: ...

    def convert(
        self,
        input_path: str,
        output_path: str,
        mode: str,
        *,
        compression: str | None = None,
        cancel_event: asyncio.Event | None = None,
    ) -> AsyncGenerator[dict, None]: ...

    async def verify(self, path: str) -> dict: ...
    def verify_stream(self, path: str) -> AsyncGenerator[dict, None]: ...
    async def info(self, path: str) -> dict: ...
    def info_model(self, raw: dict, path: str) -> BaseModel: ...

    def active_pids(self) -> list[int]: ...

    async def post_convert(
        self, input_path: str, output_path: str, mode: str,
    ) -> None: ...


class BaseTool:
    """Shared helpers so concrete tools stay tiny."""

    id: str
    display_name: str
    modes: Sequence[ModeSpec] = ()
    output_extensions: frozenset[str] = frozenset()
    verify_extensions: frozenset[str] = frozenset()

    def __init__(self, binary_path: str) -> None:
        self.binary_path = binary_path

    @property
    def input_extensions(self) -> frozenset[str]:
        if not self.modes:
            return frozenset()
        return frozenset().union(*(m.input_extensions for m in self.modes))

    def spec(self, mode: str) -> ModeSpec:
        for m in self.modes:
            if m.mode == mode:
                return m
        raise KeyError(mode)

    async def post_convert(
        self, input_path: str, output_path: str, mode: str,
    ) -> None:
        return None
