"""Synthetic PSP and 3DS containers for the rom-converto tests (#423).

Hand-built headers around a few bytes of filler, never a game. The stub
rom-converto in ``test_rom_converto_423.py`` imports this module too, so the
inputs the tests build and the products the stub writes share one shape.
"""
from __future__ import annotations

import struct

SECTOR = 2048
MEDIA_UNIT = 0x200
CIA_HEADER_SIZE = 0x2020


def _align64(n: int) -> int:
    return (n + 63) & ~63


def pbp_bytes(psar_magic: bytes = b"NPUMDIMG", payload: bytes = b"") -> bytes:
    """An EBOOT.PBP: the eight-offset header, a stub PARAM.SFO, then DATA.PSAR.

    A PSP game's PSAR starts ``NPUMDIMG``; a PS1 Classic's ``PSISOIMG``.
    """
    sfo = b"\0PSF" + b"\x01\x01\0\0" + b"\0" * 12
    sfo_off = 0x28
    psar_off = sfo_off + len(sfo)
    # PARAM.SFO, then ICON0, ICON1, PIC0, PIC1, SND0, DATA.PSP (all empty), DATA.PSAR.
    offsets = [sfo_off] + [psar_off] * 7
    head = b"\0PBP" + struct.pack("<I", 0x00010000) + struct.pack("<8I", *offsets)
    return head + sfo + psar_magic + payload


def iso_bytes(payload: bytes = b"", sectors: int = 18) -> bytes:
    """An ISO 9660 image: a primary volume descriptor at sector 16 saying
    *sectors* sectors of 2048 bytes, and *payload* in sector 17."""
    body = bytearray(sectors * SECTOR)
    pvd = bytearray(SECTOR)
    pvd[0] = 1
    pvd[1:6] = b"CD001"
    pvd[6] = 1
    pvd[8:16] = b"PSP GAME"
    struct.pack_into("<I", pvd, 0x50, sectors)
    struct.pack_into(">I", pvd, 0x54, sectors)
    struct.pack_into("<H", pvd, 0x80, SECTOR)
    struct.pack_into(">H", pvd, 0x82, SECTOR)
    body[16 * SECTOR:17 * SECTOR] = pvd
    tail = payload[:SECTOR]
    body[17 * SECTOR:17 * SECTOR + len(tail)] = tail
    return bytes(body)


def cci_bytes(payload: bytes = b"", partition_units: int = 2) -> bytes:
    """A CCI (NCSD): the header with one partition of *partition_units* media
    units right after it, an NCCH magic in the partition, and *payload*."""
    head = bytearray(MEDIA_UNIT)
    head[0x100:0x104] = b"NCSD"
    struct.pack_into("<I", head, 0x104, 1 + partition_units)
    struct.pack_into("<II", head, 0x120, 1, partition_units)
    part = bytearray(partition_units * MEDIA_UNIT)
    part[0x100:0x104] = b"NCCH"
    tail = payload[:64]
    part[0x200:0x200 + len(tail)] = tail
    return bytes(head + part)


def cia_bytes(payload: bytes = b"", content: int = 0x400) -> bytes:
    """A CIA: the 0x2020-byte archive header, then the certificate chain,
    ticket, TMD and *content* bytes, each 64-byte aligned, with *payload*."""
    cert, ticket, tmd, meta = 0xA00, 0x350, 0x208, 0
    head = bytearray(CIA_HEADER_SIZE)
    struct.pack_into(
        "<IHHIIIIQ", head, 0, CIA_HEADER_SIZE, 0, 0, cert, ticket, tmd, meta, content,
    )
    total = (
        _align64(CIA_HEADER_SIZE) + _align64(cert) + _align64(ticket)
        + _align64(tmd) + _align64(content) + _align64(meta)
    )
    body = bytearray(total)
    body[:CIA_HEADER_SIZE] = head
    start = total - _align64(content)
    tail = payload[:64]
    body[start:start + len(tail)] = tail
    return bytes(body)
