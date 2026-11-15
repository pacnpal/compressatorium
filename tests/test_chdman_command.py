"""chdman command building."""
from services.chdman import chdman_service


def test_createraw_passes_a_unit_size() -> None:
    """#326: chdman refuses createraw without --unitsize."""
    cmd = chdman_service._build_command("createraw", "/d/disk.img", "/d/disk.chd")
    assert cmd[cmd.index("-us") + 1] == "512"


def test_createraw_unit_size_survives_compression() -> None:
    cmd = chdman_service._build_command(
        "createraw", "/d/disk.img", "/d/disk.chd", compression="zstd",
    )
    assert "-us" in cmd and ["-c", "zstd"] == cmd[cmd.index("createraw") + 1:][:2]


def test_other_modes_have_no_unit_size() -> None:
    assert "-us" not in chdman_service._build_command("createhd", "/d/a.img", "/d/a.chd")
