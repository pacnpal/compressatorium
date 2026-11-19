# RAR test sets

The rar tool's tests (#476) run against these archives. They are not ROMs or
game images: every one is a test archive from the `rarfile` Python library's
own test suite, copied unchanged from
<https://github.com/markokr/rarfile/tree/master/test/files>.

`rarfile` is © Marko Kreen and licensed under the ISC license, which allows
copying and modifying with the copyright notice kept. The notice is in
`LICENSE.rarfile` beside this file.

| File(s) | What it is |
|---------|------------|
| `rar5-vols.part1.rar` … `part3.rar` | RAR5, three volumes, new naming. `vols/bigfile.txt` (205000 bytes, split across all three) and `vols/smallfile.txt` (2050 bytes) |
| `rar3-old.rar`, `.r00`, `.r01` | RAR4 (RAR 2.9 format), three volumes, old `.rNN` naming, the same two files |
| `rar5-psw.rar` | RAR5 with encrypted file data (the listing still reads) |
| `rar5-hpsw.rar` | RAR5 with encrypted headers (nothing reads without the password) |
| `rar5-evil-symlink-traversal.rar` | RAR5 with a symlink member `up -> ..` and `up/pwned.txt` behind it |
| `rar5-dups.rar` | RAR5 with one file and eight file-copy references to it |

Both members of the volume sets are `.txt` files, which the extractor treats
as release sidecars, never the payload. The tests make their own copies with
the big member renamed (`vols/bigfile.txt` to `vols/bigfile.gba`, same
length) and the header CRCs recomputed (`tests/rar_fixtures.py`). The stored
data is untouched, so its CRC still matches. Nothing here is modified on disk.
