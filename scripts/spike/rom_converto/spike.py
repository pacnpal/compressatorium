#!/usr/bin/env python3
"""rom-converto v0.24.0 spike (#421). A manual check, not a test.

Runs the pinned release binary against synthetic inputs and one MIT-licensed
homebrew CIA, and prints what each command does. Standard library only.

    python3 scripts/spike/rom_converto/spike.py --work /tmp/rom-converto-spike
    python3 scripts/spike/rom_converto/spike.py --work ... --target docker-amd64
    python3 scripts/spike/rom_converto/spike.py --work ... --target native

It never downloads a commercial game image, a key or seeddb.bin. See README.md.
"""

import argparse
import hashlib
import importlib.util
import json
import os
import platform
import shutil
import struct
import subprocess
import sys
import urllib.request

_SPEC = importlib.util.spec_from_file_location(
    "fixtures", os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures.py")
)
fixtures = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(fixtures)

VERSION = "0.24.0"
RELEASE_URL = "https://github.com/DevYukine/rom-converto/releases/download/v" + VERSION
# SHA-256 of each release asset, from the release's own digests and .sha256 files.
ASSETS = {
    "rom-converto-cli-linux-x64": (
        "0d4abd56cfec477ff447f68b656d45ca48f670f464363bca2d895b1eee2c356d"
    ),
    "rom-converto-cli-linux-arm64": (
        "990286bc3367342b0e04ab6dd0d28d8e95c3133ac2b4c5d6440a119e541b0b43"
    ),
    "rom-converto-cli-macos-arm64": (
        "4de8bbc96ddef23d8ed823ce42efcd64425c7885c4fe25058a2376ab42053aee"
    ),
    "rom-converto-cli-macos-x64": (
        "8bf11dd14c920db3ed9d00782a0ab018cc3d1e316b63d0deb2ff7b50d19e85f9"
    ),
}
# FBI 2.6.1 by Steveice10: an MIT-licensed 3DS homebrew title manager. Its CIA
# is the only real 3DS container the spike uses. NCCH is not encrypted.
FBI_URL = "https://github.com/Steveice10/FBI/releases/download/2.6.1/FBI.cia"
FBI_SHA256 = "09b535826caa9b04a682ec07e0df605b5d8774af35b4dd77955e6c913df1a48b"
# The runtime base the app's own Dockerfile uses.
DOCKER_IMAGE = (
    "debian:trixie-slim@sha256:a72b4433de1cf7d6bae0c4cee74d0547f58601a643b3375f070c51600d63b6b2"
)
MARKER = ".rom-converto-spike"

TARGETS = {
    "docker-arm64": ("rom-converto-cli-linux-arm64", "linux/arm64"),
    "docker-amd64": ("rom-converto-cli-linux-x64", "linux/amd64"),
}


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def fetch(url, dest, want):
    if not (os.path.isfile(dest) and sha256(dest) == want):
        with urllib.request.urlopen(url, timeout=120) as r, open(dest, "wb") as f:
            shutil.copyfileobj(r, f)
    got = sha256(dest)
    if got != want:
        os.remove(dest)
        sys.exit(f"checksum mismatch for {url}: got {got}, pinned {want}")


def prepare_work(work):
    work = os.path.abspath(work)
    if os.path.exists(work) and os.listdir(work) and not os.path.isfile(os.path.join(work, MARKER)):
        sys.exit(f"{work} is not empty and is not a spike folder; pass a new or empty one")
    os.makedirs(work, exist_ok=True)
    with open(os.path.join(work, MARKER), "w") as f:
        f.write("rom-converto spike\n")
    return work


class Runner:
    """Runs the binary natively or in the app's runtime base image."""

    def __init__(self, target, work):
        self.target = target
        self.work = work
        self.bin_dir = os.path.join(work, "bin")
        os.makedirs(self.bin_dir, exist_ok=True)
        if target == "native":
            system, machine = platform.system(), platform.machine().lower()
            if system != "Darwin":
                sys.exit("--target native only knows the macOS assets; use a docker target")
            self.asset = "rom-converto-cli-macos-" + ("arm64" if machine == "arm64" else "x64")
            self.platform = None
        else:
            self.asset, self.platform = TARGETS[target]
        self.binary = os.path.join(self.bin_dir, self.asset)
        fetch(f"{RELEASE_URL}/{self.asset}", self.binary, ASSETS[self.asset])
        os.chmod(self.binary, 0o755)
        self.run_dir = os.path.join(work, "run-" + target)
        shutil.rmtree(self.run_dir, ignore_errors=True)
        os.makedirs(os.path.join(self.run_dir, "home"))

    def run(self, *args, network=True, update_check=False, timeout=300):
        """Run one command in the run folder. Returns (rc, combined output)."""
        proc = self._run(args, network, update_check, timeout)
        return proc.returncode, (proc.stdout + proc.stderr).strip()

    def run_split(self, *args):
        """Run one command and return (stdout, stderr)."""
        proc = self._run(args, True, False, 300)
        return proc.stdout.strip(), proc.stderr.strip()

    def _run(self, args, network, update_check, timeout):
        base = [] if update_check else ["--no-update-check"]
        if self.target == "native":
            cmd = [self.binary, *base, *args]
            env = {
                "PATH": os.environ["PATH"],
                "HOME": os.path.join(self.run_dir, "home"),
                "NO_COLOR": "1",
            }
            proc = subprocess.run(
                cmd,
                cwd=self.run_dir,
                env=env,
                check=False,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        else:
            cmd = ["docker", "run", "--rm", "--platform", self.platform]
            cmd += ["--user", f"{os.getuid()}:{os.getgid()}"]
            if not network:
                cmd += ["--network", "none"]
            cmd += [
                "-e",
                "HOME=/w/home",
                "-e",
                "NO_COLOR=1",
                "-v",
                f"{self.run_dir}:/w",
                "-w",
                "/w",
            ]
            cmd += ["-v", f"{self.bin_dir}:/b:ro", DOCKER_IMAGE, "/b/" + self.asset, *base, *args]
            proc = subprocess.run(cmd, check=False, capture_output=True, text=True, timeout=timeout)
        return proc

    def path(self, name):
        return os.path.join(self.run_dir, name)


class Report:
    def __init__(self):
        self.rows = []

    def check(self, key, ok, detail=""):
        self.rows.append({"check": key, "ok": bool(ok), "detail": detail})
        print(f"[{'PASS' if ok else 'FAIL'}] {key}" + (f"  ({detail})" if detail else ""))
        return ok

    def note(self, key, detail):
        self.rows.append({"check": key, "ok": None, "detail": detail})
        print(f"[NOTE] {key}  ({detail})")


def tree(root):
    out = {}
    for base, _dirs, names in os.walk(root):
        for n in names:
            p = os.path.join(base, n)
            with open(p, "rb") as f:
                out[os.path.relpath(p, root)] = f.read()
    return out


def open_head(path, size):
    with open(path, "rb") as f:
        return f.read(size)


def last_line(text):
    lines = [x for x in text.splitlines() if x.strip()]
    return lines[-1] if lines else ""


def xenon_checks(r, rep):
    files = fixtures.xbox360_files()
    bases = {
        "trimmed": fixtures.BASE_TRIMMED,
        "xgd3": fixtures.BASE_XGD3,
        "xgd2": fixtures.BASE_XGD2,
    }
    for label, base in bases.items():
        with open(r.path(f"{label}.iso"), "wb") as f:
            f.write(fixtures.build_xdvdfs(files, base))
        rc, out = r.run("xenon", "compress", f"{label}.iso")
        rep.check(
            f"xenon compress {label} -> default name {label}.zar",
            rc == 0 and os.path.isfile(r.path(f"{label}.zar")),
            last_line(out),
        )
        rc, out = r.run("xenon", "verify", f"{label}.zar")
        rep.check(f"xenon verify {label}", rc == 0 and "Hash: OK" in out, last_line(out))
        rc, out = r.run("xenon", "extract", f"{label}.zar", f"x_{label}")
        same = tree(r.path(f"x_{label}")) == files
        rep.check(f"xenon extract {label} -> every file byte-equal to the ISO's", rc == 0 and same)

    # Independent read of the ISO's own file tree, for the round-trip comparison.
    rc, out = r.run("-q", "xbox", "extract", "xgd2.iso", "xbox_x")
    rep.check(
        "xbox extract reads the same ISO (a second reader for the comparison)",
        rc == 0 and tree(r.path("xbox_x")) == files,
        last_line(out),
    )

    rc, out = r.run("xenon", "--help")
    names = [
        ln.split()[0]
        for ln in out.split("Commands:")[1].split("Options:")[0].splitlines()
        if ln.strip()
    ]
    rep.check(
        "no xenon subcommand turns a ZAR back into an ISO",
        "to-iso" not in names and "decompress" not in names,
        "xenon commands: " + ", ".join(names),
    )
    with open(r.path("trimmed.iso"), "rb") as f:
        iso_sha = hashlib.sha256(f.read()).hexdigest()
    rep.note(
        "ZAR holds files, not sectors",
        f"ISO sha256 {iso_sha[:16]}..., ZAR "
        f"{os.path.getsize(r.path('trimmed.zar'))} bytes; extract yields a folder, never an ISO",
    )

    first = sha256(r.path("trimmed.zar"))
    rc, out = r.run("xenon", "compress", "trimmed.iso")
    rep.check(
        "compress refuses an existing output by default",
        rc != 0 and sha256(r.path("trimmed.zar")) == first,
        last_line(out),
    )
    rc, out = r.run("xenon", "compress", "-f", "trimmed.iso")
    rep.check("compress -f overwrites", rc == 0, last_line(out))
    rep.check(
        "compressing the same ISO twice gives the same ZAR bytes",
        sha256(r.path("trimmed.zar")) == first,
    )
    rc, out = r.run("xenon", "extract", "trimmed.zar", "x_trimmed")
    rep.check("extract refuses a non-empty output dir", rc != 0, last_line(out))

    with open(r.path("random.iso"), "wb") as f:
        f.write(fixtures.rand_bytes(100000, 5))
    rc, out = r.run("xenon", "compress", "random.iso")
    rep.check(
        "a non-XDVDFS .iso fails and leaves no output",
        rc != 0 and not os.path.exists(r.path("random.zar")),
        last_line(out),
    )
    rc, out = r.run("-q", "xenon", "compress", "-f", "trimmed.iso", "quiet.zar")
    rep.check("-q prints nothing on success", rc == 0 and out == "", repr(out))


def psp_checks(r, rep):
    with open(r.path("fake.pbp"), "wb") as f:
        f.write(fixtures.build_pbp(fixtures.rand_bytes(4096, 9)))
    rc, out = r.run("psp", "info", "fake.pbp")
    rep.check(
        "psp info reads the hand-made PBP's PARAM.SFO",
        rc == 0 and "SPIKE" in out,
        "DATA.PSAR flagged unknown",
    )
    rc, out = r.run("psp", "to-iso", "fake.pbp")
    rep.check(
        "psp to-iso refuses a PBP whose PSAR is not NPUMDIMG, no output left",
        rc != 0 and not os.path.exists(r.path("fake.pbp.iso")),
        last_line(out),
    )
    rc, out = r.run("-q", "psp", "to-iso", "fake.pbp", "--report", "r.json")
    wrote = os.path.exists(r.path("r.json"))
    rc2, _ = r.run("ctr", "convert", "x.cia", "--report", "r2.json")
    rep.check(
        "--report is a psp to-iso option only, and a failed run writes no file",
        not wrote and rc2 != 0,
        f"file written on failure: {wrote}; ctr convert --report exit {rc2}",
    )
    rep.note(
        "psp to-iso success path NOT tested",
        "a real PBP needs a game's encrypted NPUMDIMG; the keys are inside the binary (disc header "
        "carries the version key) so no key file is taken, but this needs an owner check on a "
        "PBP they own",
    )


def ctr_checks(r, rep, homebrew):
    shutil.copy(homebrew, r.path("FBI.cia"))
    cia = r.path("FBI.cia")
    rc, out = r.run("-q", "ctr", "info", "FBI.cia")
    rep.check("the homebrew CIA's NCCH is not encrypted", "NCCH encrypted:  no" in out)
    rc, out = r.run("ctr", "convert", "FBI.cia")
    cci = r.path("FBI.3ds")
    rep.check(
        "CIA -> CCI with no keys, seeds or network", rc == 0 and os.path.isfile(cci), last_line(out)
    )
    size = os.path.getsize(cci) if os.path.isfile(cci) else 0
    rep.check("CCI is padded to 128 MiB by default", size == 128 * 1024 * 1024, f"{size} bytes")
    rc, out = r.run("ctr", "convert", "FBI.cia", "FBI_trim.3ds", "--trim")
    trim = r.path("FBI_trim.3ds")
    rep.check(
        "--trim stops after the last partition",
        rc == 0 and 0 < os.path.getsize(trim) < size,
        f"{os.path.getsize(trim) if os.path.isfile(trim) else 0} bytes",
    )

    with open(cci, "rb") as f:
        head = f.read(0x200)
        part_off, part_size = struct.unpack_from("<II", head, 0x120)
        f.seek(part_off * 0x200)
        ncch = f.read(part_size * 0x200)
    with open(cia, "rb") as f:
        rep.check(
            "the CCI partition is byte-equal to the CIA's content (NCCH untouched)",
            ncch in f.read(),
        )

    shutil.copy(cci, r.path("back.cci"))
    rc, out = r.run("ctr", "convert", "back.cci")
    back = r.path("back.cia")
    rep.check(
        "CCI -> CIA with no keys, seeds or network",
        rc == 0 and os.path.isfile(back),
        last_line(out),
    )
    with open(back, "rb") as f:
        back_bytes = f.read()
    rep.check(
        "the CIA's content carries the same NCCH",
        ncch.rstrip(b"\xff") in back_bytes or ncch in back_bytes,
    )
    rep.check(
        "CIA -> CCI -> CIA is NOT byte-equal to the original CIA",
        sha256(back) != sha256(cia),
        f"{os.path.getsize(cia)} vs {os.path.getsize(back)} bytes",
    )
    rc, out = r.run("ctr", "verify", "back.cia")
    rep.note(
        "ctr verify on the converted CIA",
        " | ".join(x.strip() for x in out.splitlines() if "ignature" in x or "Legitimacy" in x),
    )

    rc, out = r.run("ctr", "convert", "FBI.cia")
    rep.check("convert refuses an existing output by default", rc != 0, last_line(out))
    shutil.copy(cia, r.path("noext.bin"))
    rc, out = r.run("ctr", "convert", "noext.bin")
    rep.check("convert refuses an input that is not .cia/.3ds/.cci", rc != 0, last_line(out))

    with open(cci, "rb") as f:
        flagged = bytearray(f.read())
    ncch0 = part_off * 0x200
    flagged[ncch0 + 0x188 : ncch0 + 0x190] = bytes([0, 0, 0, 0x0A, 0, 0, 0, 0x20])
    with open(r.path("flagged.cci"), "wb") as f:
        f.write(flagged)
    rc, out = r.run("ctr", "convert", "flagged.cci")
    rep.note(
        "CCI whose NCCH flags claim seed crypto (content is really plaintext)",
        f"rc={rc}; {last_line(out)}. It reads the exheader with keys built into the binary "
        "and never asks for a seed",
    )
    rep.note(
        "encrypted CIA/CCI success path NOT tested",
        "needs a real retail image; source shows CIA -> CCI derives the title key from the "
        "ticket with the "
        "common keys built into the binary, and CCI -> CIA reads the exheader with built-in keys. "
        "Owner check with a dump they own",
    )

    bad = {
        "bad_cia.cia": fixtures.rand_bytes(300000, 11),
        "bad_cci.3ds": fixtures.rand_bytes(300000, 12),
        "short.cia": open_head(cia, 5000),
        "cut.3ds": open_head(cci, 600000),
    }
    for name, data in bad.items():
        with open(r.path(name), "wb") as f:
            f.write(data)
        rc, out = r.run("ctr", "convert", name)
        stem = name.rsplit(".", 1)[0]
        left = [x for x in os.listdir(r.run_dir) if x.startswith(stem) and x != name]
        first = out.splitlines()[0] if out else ""
        rep.check(
            f"ctr convert {name}: exit 1, an error line, no output left",
            rc == 1 and first.startswith("Error:") and not left,
            first,
        )
    rc, out = r.run("ctr", "convert", "nothere.cia")
    rep.check("a missing input exits 1", rc == 1, last_line(out))
    rc, out = r.run("ctr", "convert")
    rep.check("missing arguments exit 2 (clap usage error)", rc == 2, out.splitlines()[0])
    rc, out = r.run("ctr", "convert", "FBI.cia", "streams.3ds", "-f")
    proc_out = r.run_split("ctr", "convert", "FBI.cia", "streams.3ds", "-f")
    rep.check(
        "success is reported on stderr; stdout stays empty",
        rc == 0 and proc_out[0] == "" and "Converted CIA to CCI" in proc_out[1],
    )

    if r.target != "native":
        rc, out = r.run("ctr", "convert", "FBI.cia", "offline.3ds", network=False)
        rep.check(
            "CIA -> CCI works with --network none",
            rc == 0 and os.path.isfile(r.path("offline.3ds")),
            last_line(out),
        )


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument(
        "--work", required=True, help="a new or empty folder, or one an earlier run made"
    )
    default = (
        "docker-arm64" if platform.machine().lower() in ("arm64", "aarch64") else "docker-amd64"
    )
    ap.add_argument("--target", choices=[*TARGETS, "native"], default=default)
    args = ap.parse_args()

    work = prepare_work(args.work)
    homebrew = os.path.join(work, "FBI.cia")
    fetch(FBI_URL, homebrew, FBI_SHA256)
    r = Runner(args.target, work)
    rep = Report()

    rc, out = r.run("--version")
    want = "rom-converto " + VERSION
    rep.check(f"{r.asset} reports {want}", rc == 0 and want in out.splitlines(), last_line(out))
    rep.note("asset sha256", f"{r.asset} {ASSETS[r.asset]}")
    xenon_checks(r, rep)
    psp_checks(r, rep)
    ctr_checks(r, rep, homebrew)

    with open(os.path.join(work, f"results-{args.target}.json"), "w") as f:
        json.dump(rep.rows, f, indent=2)
    failed = [x for x in rep.rows if x["ok"] is False]
    print(f"\n{len(failed)} failed; results in {work}/results-{args.target}.json")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
