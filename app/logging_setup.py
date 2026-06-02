"""Single source of truth for the application logger.

All app loggers hang off one root name so a single handler/level (configured in
``main.configure_logging``) governs the whole app. Modules should call
``get_logger("area")`` rather than hardcoding the root — e.g. ``get_logger("nsz")``
yields the ``compressatorium.nsz`` logger.
"""
from __future__ import annotations

import logging

# The app's root logger name. (Historically "chd", from the chdman-only days.)
APP_LOGGER = "compressatorium"


def get_logger(name: str | None = None) -> logging.Logger:
    """Return the app root logger, or a ``compressatorium.<name>`` child."""
    if not name:
        return logging.getLogger(APP_LOGGER)
    return logging.getLogger(f"{APP_LOGGER}.{name}")
