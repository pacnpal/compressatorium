import re
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def _read_repo_file(relative_path: str) -> str:
    return (REPO_ROOT / relative_path).read_text(encoding="utf-8")


def test_dockerfile_uses_gosu_and_no_static_user_directive():
    dockerfile = _read_repo_file("Dockerfile")

    assert re.search(
        r'apt-get install -y --no-install-recommends[\s\S]*?\bgosu\b',
        dockerfile,
    )
    assert re.search(r'HEALTHCHECK[\s\S]*CHD_MODE:-webui', dockerfile)
    assert re.search(r'HEALTHCHECK[\s\S]*\$\(\s*id -u\s*\)\s*"\s*=\s*"0"', dockerfile)
    assert re.search(r'HEALTHCHECK[\s\S]*CMD[\s\S]*gosu\s+converter\s+python3\s+-c', dockerfile)
    assert re.search(r'HEALTHCHECK[\s\S]*else\s+python3\s+-c', dockerfile)
    assert re.search(r'HEALTHCHECK[\s\S]*\|\|\s+exit\s+0', dockerfile) is None
    assert re.search(r'groupadd\s+-r\s+-g\s+999\s+converter', dockerfile)
    assert re.search(r'useradd\s+-r\s+-u\s+999\s+-g\s+converter', dockerfile)
    assert "ENTRYPOINT [\"/entrypoint.sh\"]" in dockerfile
    assert re.search(r"^\s*USER\s+converter\s*$", dockerfile, flags=re.MULTILINE) is None


def test_entrypoint_remaps_uid_gid_before_dropping_privileges():
    entrypoint = _read_repo_file("entrypoint.sh")

    assert re.search(r'if\s+\[\s*"\$\(id -u\)"\s*=\s*"0"\s*\]\s*;\s*then', entrypoint)
    assert re.search(r'PUID=\$\{PUID:-\d+\}', entrypoint)
    assert re.search(r'PGID=\$\{PGID:-\d+\}', entrypoint)
    assert re.search(r'\[\s*"\$PUID"\s*-eq\s*0\s*\]', entrypoint)
    assert re.search(r'\[\s*"\$PGID"\s*-eq\s*0\s*\]', entrypoint)
    assert re.search(r'Both must be numeric and greater than 0', entrypoint)
    assert re.search(r'groupmod\s+-g\s+"\$PGID"\s+converter', entrypoint)
    assert re.search(r'getent\s+group\s+"\$PGID"\s+>/dev/null', entrypoint)
    assert re.search(r'Failed to remap converter to PGID', entrypoint)
    assert re.search(r'getent\s+passwd\s+"\$PUID"\s+>/dev/null', entrypoint)
    assert re.search(r'Cannot remap converter to PUID', entrypoint)
    assert re.search(r'usermod\s+-g\s+"\$PGID"\s+converter', entrypoint)
    assert re.search(r'usermod\s+-u\s+"\$PUID"\s+converter', entrypoint)
    assert re.search(r'for\s+optional_path\s+in\s+/config\s+/data/games;\s+do', entrypoint)
    assert re.search(r'skip_optional_path=0', entrypoint)
    assert re.search(r'findmnt\s+-n\s+-o\s+OPTIONS\s+--target\s+"\$optional_path"', entrypoint)
    assert re.search(r'Warning: unable to determine mount options for', entrypoint)
    assert re.search(r'echo\s+"\$mount_opts"\s+\|\s+grep\s+-Eqw\s+\'bind\|rbind\'', entrypoint)
    assert re.search(r'\[\s*"\$skip_optional_path"\s+-eq\s+0\s*\]', entrypoint)
    assert re.search(r'chown\s+-R\s+converter:"\$\(\s*id -g converter\s*\)"\s+"\$\{paths_to_chown\[@\]\}"', entrypoint)
    assert re.search(r'exec\s+gosu\s+converter\s+"\$0"\s+"\$@"', entrypoint)
    assert re.search(
        r'elif\s+\[\s+-n\s+"\$\{PUID:-\}"\s+\]\s+\|\|\s+\[\s+-n\s+"\$\{PGID:-\}"\s+\]\s*;\s*then',
        entrypoint,
    )
    assert re.search(r'PUID/PGID remap requires container startup as root', entrypoint)

    assert entrypoint.index("groupmod") < entrypoint.index("usermod -u")
    assert entrypoint.index("usermod -u") < entrypoint.index("chown -R")
    assert entrypoint.index("chown -R") < entrypoint.index("exec gosu converter")
