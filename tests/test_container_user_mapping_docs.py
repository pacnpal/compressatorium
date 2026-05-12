from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def _read_repo_file(relative_path: str) -> str:
    return (REPO_ROOT / relative_path).read_text(encoding="utf-8")


def test_dockerfile_uses_gosu_and_no_static_user_directive():
    dockerfile = _read_repo_file("Dockerfile")

    assert "gosu \\" in dockerfile
    assert "ENTRYPOINT [\"/entrypoint.sh\"]" in dockerfile
    assert "\nUSER converter\n" not in dockerfile


def test_entrypoint_remaps_uid_gid_before_dropping_privileges():
    entrypoint = _read_repo_file("entrypoint.sh")

    assert "if [ \"$(id -u)\" = \"0\" ]; then" in entrypoint
    assert "PUID=${PUID:-999}" in entrypoint
    assert "PGID=${PGID:-999}" in entrypoint
    assert "groupmod -g \"$PGID\" converter 2>/dev/null" in entrypoint
    assert "usermod -g \"$PGID\" converter" in entrypoint
    assert "usermod -u \"$PUID\" converter" in entrypoint
    assert "ownership_changed=0" in entrypoint
    assert "if [ \"$ownership_changed\" = \"1\" ]; then" in entrypoint
    assert "exec gosu converter \"$0\" \"$@\"" in entrypoint
