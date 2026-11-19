"""A byte count as the info payloads show it, in one place."""
from __future__ import annotations


def format_size(size: int) -> str:
    """``"12.34 MB"`` below a GiB, else ``"1.23 GB"``."""
    size_mb = size / (1024 * 1024)
    return f"{size_mb:.2f} MB" if size_mb < 1024 else f"{size_mb / 1024:.2f} GB"
