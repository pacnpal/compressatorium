"""Build the spike's test Images. Runs INSIDE the fixture container (xorriso).

Usage: python3 build_fixtures.py OUT_DIR

Each Image is small and built the way the smoke harness builds its fixtures:
ISO 9660 by xorriso, a PS2 SYSTEM.CNF naming the boot ELF (the serial RomM's
title-id reader looks for), a PSP UMD_DATA.BIN and PARAM.SFO, and a GameCube
disc header with a game id and a valid FST. Every Image gets its own serial,
so RomM can tell them apart by title id as well as by hash. RomM's GameCube
title id is the first four bytes of the game id, so those differ too.

Writes OUT_DIR/<platform>/<name>.iso for every entry in FIXTURES.
"""
import base64
import os
import random
import shutil
import struct
import subprocess
import sys

STAGE = "/tmp/stage"
rng = random.Random(382)

# (platform folder, file stem, serial or game id). The stems name the
# experiment each Image is used for; see spike.py.
FIXTURES = [
    ("ps2", "Swap PS2", "SLUS_382.01"),
    ("ps2", "QuickSwap PS2", "SLUS_382.02"),
    ("ps2", "Stealth PS2", "SLUS_382.03"),
    ("ps2", "Reid PS2 CHD", "SLUS_382.04"),
    ("ps2", "Reid PS2 CSO", "SLUS_382.05"),
    ("ps2", "Reid PS2 ZSO", "SLUS_382.06"),
    ("ps2", "Rename PS2", "SLUS_382.07"),
    ("ps2", "Refuse PS2", "SLUS_382.08"),
    ("ps2", "Orphan PS2", "SLUS_382.09"),
    ("ps2", "Race PS2 CHD", "SLUS_382.10"),
    ("ngc", "Swap GC", "GSWA01"),
    ("ngc", "Reid GC RVZ", "GRVA01"),
    ("ngc", "Reid GC WIA", "GWIA01"),
    ("ngc", "Reid GC GCZ", "GGCA01"),
    ("psp", "Swap PSP", "ULUS38201"),
    ("psp", "Reid PSP CSO", "ULUS38202"),
    ("psp", "Reid PSP ZSO", "ULUS38203"),
    ("psp", "Reid PSP CHD", "ULUS38204"),
]


def mkdir(p):
    os.makedirs(p, exist_ok=True)


def filler(n):
    """Deterministic, compressible bytes (base64 text)."""
    return base64.b64encode(rng.randbytes((n * 3) // 4 + 4))[:n]


def iso(out, files, volid, sysid=None):
    """files: {relpath: bytes}, built with xorriso -as mkisofs."""
    if os.path.exists(STAGE):
        shutil.rmtree(STAGE)
    for rel, data in files.items():
        p = os.path.join(STAGE, rel)
        mkdir(os.path.dirname(p))
        with open(p, "wb") as f:
            f.write(data)
    mkdir(os.path.dirname(out))
    cmd = ["xorriso", "-as", "mkisofs", "-quiet", "-iso-level", "2", "-V", volid, "-o", out]
    if sysid:
        cmd += ["-sysid", sysid]
    subprocess.run(cmd + [STAGE], check=True)


def ps2_dvd(path, serial):
    cnf = f"BOOT2 = cdrom0:\\{serial};1\r\nVER = 1.00\r\nVMODE = NTSC\r\n"
    iso(path, {"SYSTEM.CNF": cnf.encode(), serial: filler(2 * 1024 * 1024)},
        volid="PS2SPIKE", sysid="PLAYSTATION")


def param_sfo(entries):
    """Minimal PSF with utf8 strings (fmt 0x0204)."""
    keys = datas = index = b""
    for k, v in entries.items():
        kb, vb = k.encode() + b"\x00", v.encode() + b"\x00"
        vmax = (len(vb) + 3) & ~3
        index += struct.pack("<HHIII", len(keys), 0x0204, len(vb), vmax, len(datas))
        keys += kb
        datas += vb.ljust(vmax, b"\x00")
    keys = keys.ljust((len(keys) + 3) & ~3, b"\x00")
    key_off = 20 + len(index)
    hdr = b"\x00PSF" + struct.pack("<IIII", 0x0101, key_off, key_off + len(keys), len(entries))
    return hdr + index + keys + datas


def psp_iso(path, disc_id):
    sfo = param_sfo({"CATEGORY": "UG", "DISC_ID": disc_id, "DISC_VERSION": "1.00",
                     "TITLE": f"SPIKE {disc_id}"})
    iso(path, {"UMD_DATA.BIN": f"{disc_id[:4]}-{disc_id[4:]}|0000000000000001|0001|G".encode(),
               "PSP_GAME/PARAM.SFO": sfo,
               "PSP_GAME/SYSDIR/EBOOT.BIN": filler(512 * 1024),
               "PSP_GAME/USRDIR/DATA.BIN": filler(1024 * 1024)},
        volid="UMD_SPIKE", sysid="PSP GAME")


def gamecube_iso(path, game_id):
    """A 4 MiB GameCube disc: header, apploader, a DOL and a one-file FST."""
    disc = bytearray(4 * 1024 * 1024)
    title = f"SPIKE {game_id}".encode()
    disc[0:6] = game_id.encode()
    struct.pack_into(">I", disc, 0x1C, 0xC2339F3D)
    disc[0x20:0x20 + len(title)] = title
    dol_off, fst_off, file_off = 0x10000, 0x20000, 0x30000
    payload = filler(1024 * 1024)
    names = b"\x00" + b"opening.bnr\x00"
    fst = struct.pack(">BBHII", 1, 0, 0, 0, 2)
    fst += struct.pack(">BBHII", 0, 0, 1, file_off, len(payload))
    fst += names
    struct.pack_into(">III", disc, 0x420, dol_off, fst_off, len(fst))
    struct.pack_into(">I", disc, 0x42C, len(fst))
    disc[0x2440:0x2450] = b"2026/11/16\x00\x00\x00\x00\x00\x00"
    struct.pack_into(">III", disc, 0x2450, 0x81200000, 0x20, 0)
    disc[0x2460:0x2480] = b"\x4e\x80\x00\x20" * 8
    struct.pack_into(">I", disc, dol_off, 0x100)
    struct.pack_into(">I", disc, dol_off + 0x48, 0x80003100)
    struct.pack_into(">I", disc, dol_off + 0x90, 0x100)
    struct.pack_into(">I", disc, dol_off + 0xE0, 0x80003100)
    disc[dol_off + 0x100:dol_off + 0x200] = b"\x4e\x80\x00\x20" * 64
    disc[fst_off:fst_off + len(fst)] = fst
    disc[file_off:file_off + len(payload)] = payload
    mkdir(os.path.dirname(path))
    with open(path, "wb") as f:
        f.write(disc)


BUILDERS = {"ps2": ps2_dvd, "psp": psp_iso, "ngc": gamecube_iso}


def main():
    out = sys.argv[1]
    for platform, stem, ident in FIXTURES:
        path = os.path.join(out, platform, f"{stem}.iso")
        BUILDERS[platform](path, ident)
        print(path)


if __name__ == "__main__":
    main()
