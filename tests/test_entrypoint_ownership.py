"""entrypoint.sh's PUID/PGID ownership fix-up, run for real (#350).

The root block runs as root and calls ``usermod``, ``chown`` and ``gosu`` by
absolute path, so the test runs a copy of the script with every
``/usr/bin/`` and ``/usr/sbin/`` command pointed at a fake (``find`` and
``grep`` stay real), and the image's folders pointed at temp folders. The fake
``chown`` records what it was asked to do and makes SQLite's ``-shm`` file
vanish the moment it is reached, as a second container using the same
``/config`` does.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
BASH = shutil.which("bash")
FIND = shutil.which("find")
GREP = shutil.which("grep")

pytestmark = pytest.mark.skipif(
    BASH is None or FIND is None or GREP is None or os.geteuid() == 0,
    reason="needs bash, find and grep, and a non-root user",
)

IMAGE_PATHS = ("/opt/venv", "/data/games", "/app", "/static", "/config")
_IMAGE_PATH = re.compile(
    r"(?<![\w/.:-])(" + "|".join(re.escape(p) for p in IMAGE_PATHS) + r")(?![\w/.-])"
)

FAKES = {
    "id": """
case "$*" in
  "-u") echo 0 ;;
  "-u converter") cat "$STATE/uid" 2>/dev/null || echo 999 ;;
  "-g converter") cat "$STATE/gid" 2>/dev/null || echo 999 ;;
  *) echo 0 ;;
esac
""",
    "usermod": """
case "$1" in
  -u) echo "$2" > "$STATE/uid" ;;
  -g) echo "$2" > "$STATE/gid" ;;
esac
""",
    "groupmod": 'echo "$2" > "$STATE/gid"\n',
    "getent": "exit 2\n",
    "mountpoint": '[ "$1" = "-q" ] && shift\ngrep -qxF "$1" "$STATE/mounts" 2>/dev/null\n',
    "findmnt": """
case "$*" in
  *OPTIONS*) echo "rw,relatime" ;;
  *) echo "/srv/host/folder" ;;
esac
""",
    "gosu": 'echo "$*" >> "$STATE/gosu"\n',
    # Records "<flag> <owner> <path>" per entry it changes. A path ending in
    # -shm vanishes as it is reached (ENOENT); one named "denied" fails with
    # EPERM. The first call also makes a -wal file appear in /config.
    "chown": """
flag=""
case "$1" in -R|-h) flag="$1"; shift ;; esac
owner="$1"; shift
status=0
if [ ! -e "$STATE/appeared" ]; then
  : > "$STATE/appeared"
  : > "$CONFIG/compressatorium.db-wal"
fi
for p in "$@"; do
  if [ "$flag" = "-R" ]; then
    for gone in $(find "$p" -name '*-shm'); do
      rm -f "$gone"
      echo "chown: cannot access '$gone': No such file or directory" >&2
      status=1
    done
    find "$p" | while IFS= read -r entry; do
      printf '%s %s %s\\n' "$flag" "$owner" "$entry" >> "$LOG"
    done
    continue
  fi
  case "$p" in
    *-shm)
      rm -f "$p"
      echo "chown: cannot access '$p': No such file or directory" >&2
      status=1 ;;
    */denied)
      echo "chown: changing ownership of '$p': Operation not permitted" >&2
      status=1 ;;
    *) printf '%s %s %s\\n' "$flag" "$owner" "$p" >> "$LOG" ;;
  esac
done
exit $status
""",
}


class Box:
    """A copy of entrypoint.sh wired to fakes and temp folders."""

    def __init__(self, tmp_path: Path) -> None:
        self.bin = tmp_path / "bin"
        self.state = tmp_path / "state"
        self.log = tmp_path / "chown.log"
        self.bin.mkdir()
        self.state.mkdir()
        self.roots = {
            image: tmp_path / "root" / image.strip("/").replace("/", "_")
            for image in IMAGE_PATHS
        }
        for root in self.roots.values():
            root.mkdir(parents=True)
        (self.bin / "find").symlink_to(FIND)
        (self.bin / "grep").symlink_to(GREP)
        for name, body in FAKES.items():
            path = self.bin / name
            path.write_text("#!/bin/sh\n" + body)
            path.chmod(0o755)
        text = (REPO_ROOT / "entrypoint.sh").read_text(encoding="utf-8")
        text = _IMAGE_PATH.sub(lambda m: str(self.roots[m.group(1)]), text)
        text = text.replace("/usr/sbin/", f"{self.bin}/").replace("/usr/bin/", f"{self.bin}/")
        self.script = tmp_path / "entrypoint.sh"
        self.script.write_text(text)

    @property
    def config(self) -> Path:
        return self.roots["/config"]

    def mount(self, image: str) -> None:
        with open(self.state / "mounts", "a", encoding="utf-8") as handle:
            handle.write(f"{self.roots[image]}\n")

    def run(self, puid: int, pgid: int) -> subprocess.CompletedProcess:
        env = {
            "PATH": f"{self.bin}:/usr/bin:/bin",
            "PUID": str(puid),
            "PGID": str(pgid),
            "STATE": str(self.state),
            "LOG": str(self.log),
            "CONFIG": str(self.config),
        }
        return subprocess.run(
            [BASH, str(self.script)], env=env, capture_output=True, text=True,
            timeout=60, check=False,
        )

    def changed(self) -> list[tuple[str, ...]]:
        if not self.log.exists():
            return []
        return [tuple(line.split(" ", 2)) for line in self.log.read_text().splitlines()]

    def dropped_privileges(self) -> bool:
        return (self.state / "gosu").exists()


@pytest.fixture(name="box")
def _box(tmp_path):
    return Box(tmp_path)


def _sqlite_files(config: Path) -> list[Path]:
    (config / "temp").mkdir()
    files = [
        config / "compressatorium.db",
        config / "compressatorium.db-shm",
        config / "temp" / "job.log",
    ]
    for path in files:
        path.write_bytes(b"x")
    return files


def test_a_file_that_vanishes_during_the_fix_up_does_not_stop_the_container(box):
    db, shm, job_log = _sqlite_files(box.config)
    (box.roots["/app"] / "main.py").write_bytes(b"x")

    result = box.run(4242, 4243)

    assert result.returncode == 0, result.stderr
    assert box.dropped_privileges()
    assert not shm.exists()
    changed = box.changed()
    for path in (db, job_log, box.config, box.roots["/app"] / "main.py"):
        assert ("-h", "4242:4243", str(path)) in changed
    assert all(flag == "-h" for flag, _owner, _path in changed)


def test_entries_that_already_have_the_owner_are_left_alone(box):
    _sqlite_files(box.config)
    (box.roots["/app"] / "main.py").write_bytes(b"x")

    result = box.run(os.getuid(), os.getgid())

    assert result.returncode == 0, result.stderr
    assert box.changed() == []
    assert box.dropped_privileges()


def test_any_other_ownership_error_is_shown_and_stops_the_container(box):
    (box.config / "denied").write_bytes(b"x")

    result = box.run(4242, 4243)

    assert result.returncode != 0
    assert "Operation not permitted" in result.stderr
    assert not box.dropped_privileges()


def test_a_symlink_is_changed_itself_and_never_followed(box, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.bin").write_bytes(b"x")
    link = box.config / "library"
    link.symlink_to(outside)

    result = box.run(4242, 4243)

    assert result.returncode == 0, result.stderr
    changed = box.changed()
    assert ("-h", "4242:4243", str(link)) in changed
    assert not any(path.startswith(str(outside)) for _flag, _owner, path in changed)


def test_a_mounted_library_is_never_rewritten_but_a_mounted_config_is(box):
    """findmnt's OPTIONS never list "bind", so the old check never matched."""
    box.mount("/data/games")
    box.mount("/config")
    game = box.roots["/data/games"] / "game.iso"
    game.write_bytes(b"x")
    db = box.config / "compressatorium.db"
    db.write_bytes(b"x")

    result = box.run(4242, 4243)

    assert result.returncode == 0, result.stderr
    changed = {path for _flag, _owner, path in box.changed()}
    assert str(db) in changed
    assert not any(path.startswith(str(box.roots["/data/games"])) for path in changed)
