"""The split-part rule lives on the registry (#306): ``ToolRegistry.split_owner``."""
from __future__ import annotations

from services.tools import registry


def test_split_owner_knows_a_split_part_from_a_numbered_file(tmp_path):
    (tmp_path / "save.0").write_bytes(b"save data")
    for idx in range(2):
        (tmp_path / f"Game.iso.{idx}").write_bytes(b"\0" * 4096)

    assert registry.split_owner(str(tmp_path / "save.0")) is None
    assert registry.split_owner(str(tmp_path / "Game.iso.0")) == (
        str(tmp_path / "Game.iso"), "folder_to_iso",
    )
    assert registry.split_members(str(tmp_path / "Game.iso.0")) == [
        str(tmp_path / "Game.iso.1"),
    ]
