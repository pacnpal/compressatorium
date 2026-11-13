"""Integrity check for the vendored nkit2iso source in ``third_party/nkit2iso``.

The upstream repository (github.com/DonMikone/nkit2iso) is gone, so the Docker
image builds nkit2iso from a vendored copy. That copy must stay byte-for-byte
the module zip the Go checksum database recorded for the pinned commit,
otherwise the binary we ship is no longer the audited upstream code.

``h1:`` is Go's ``dirhash.Hash1``: SHA-256 over the sorted lines
``"<sha256 hex>  <module>@<version>/<path>\\n"`` for every file in the module,
base64-encoded. Anyone can confirm the expected value independently:

    curl https://sum.golang.org/lookup/github.com/!don!mikone/nkit2iso@<VERSION>

See ``third_party/README.md`` for the full provenance record.
"""

from __future__ import annotations

import base64
import hashlib
from pathlib import Path

VENDOR_DIR = Path(__file__).resolve().parents[1] / "third_party" / "nkit2iso"
MODULE = "github.com/DonMikone/nkit2iso"
VERSION = "v0.1.1-0.20260718122450-b97129c157ed"
# From sum.golang.org (tree note 58502047) and `go mod download -json`.
EXPECTED_H1 = "h1:FDV/0eL7S5mpYiTNIh3yKmW5nS9U/8GijNONpQdn+Ro="


def _go_dirhash_h1(root: Path, prefix: str) -> str:
    files = sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file())
    summary = "".join(
        f"{hashlib.sha256((root / name).read_bytes()).hexdigest()}  {prefix}/{name}\n"
        for name in files
    )
    return "h1:" + base64.b64encode(hashlib.sha256(summary.encode()).digest()).decode()


def test_vendored_nkit2iso_matches_go_checksum_database():
    assert VENDOR_DIR.is_dir(), f"missing vendored source at {VENDOR_DIR}"
    assert _go_dirhash_h1(VENDOR_DIR, f"{MODULE}@{VERSION}") == EXPECTED_H1, (
        "third_party/nkit2iso no longer matches the upstream module recorded in "
        "sum.golang.org. Do not edit vendored files; re-vendor a new version "
        "instead (see third_party/README.md)."
    )


BIN_DIR = VENDOR_DIR.parent / "nkit2iso-bin"
COMMIT = "b97129c157edf704da5bbf65e6d9e642c843b922"


def test_hosted_binaries_match_sha256sums_and_vendored_commit():
    """The prebuilt binaries are the ones shipped in the 4.5.0-era image.

    SHA256SUMS pins their bytes, and each binary's embedded Go build info must
    name the vendored module version and commit.
    """
    lines = (BIN_DIR / "SHA256SUMS").read_text(encoding="utf-8").splitlines()
    sums = dict(reversed(line.split("  ", 1)) for line in lines if line.strip())
    assert set(sums) == {"linux_amd64/nkit2iso", "linux_arm64/nkit2iso"}
    for rel, expected in sums.items():
        data = (BIN_DIR / rel).read_bytes()
        assert hashlib.sha256(data).hexdigest() == expected, rel
        assert f"{MODULE}\t{VERSION}".encode() in data, rel
        assert f"vcs.revision={COMMIT}".encode() in data, rel


def test_dockerfile_version_stamp_matches_vendored_commit():
    dockerfile = (VENDOR_DIR.parents[1] / "Dockerfile").read_text(encoding="utf-8")
    commit_prefix = VERSION.rsplit("-", 1)[1]
    stamp = next(
        line.split("=", 1)[1].strip()
        for line in dockerfile.splitlines()
        if line.startswith("ARG NKIT2ISO_REF=")
    )
    assert stamp.startswith(commit_prefix), (
        f"Dockerfile NKIT2ISO_REF={stamp} does not match vendored {VERSION}"
    )
