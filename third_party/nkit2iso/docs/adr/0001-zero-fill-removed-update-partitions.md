# Zero-fill removed Wii update partitions instead of requiring recovery files

> Amended by [0002](0002-offer-recovery-file-download.md): the tool now also
> offers to download the recovery file for a bit-exact restore; zero-filling
> remains the fallback and the `-recovery none` behaviour.

Wii NKit images whose update partition was removed at shrink time (header word
0x218 ≠ 0) do not contain that partition's bytes, so a bit-exact restore is
impossible from the file alone. We restore them anyway: the original partition
table is taken from the backup NKit keeps in the first 0x100 bytes of the
32 KiB placeholder at 0x50000, the update region is zero-filled up to the first
surviving partition's original offset, the CRC32 verification is skipped, and a
warning is printed. This mirrors exactly what official NKit does when its
recovery files are missing, so our output is identical to its "recoverable"
output.

## Considered options

- **Require / auto-download recovery files** (they are publicly archived on
  archive.org, keyed by the CRC32 at 0x218, and would make the restore
  bit-exact). Rejected for now: keeps the tool offline and dependency-free, and
  a playable ISO was the explicitly chosen goal. Verified feasible — splicing
  the matching recovery file into our zero-filled region at 0x50000 reproduces
  the exact redump CRC32 — so this can be added later without reworking the
  restore path.
- **Keep refusing these images** (previous behaviour). Rejected: official NKit
  demonstrates a useful playable output exists.

## Consequences

- Exit code 0 with a skipped CRC check is now a possible outcome; scripts that
  relied on "exit 0 ⇒ redump-verified" must parse the warning instead.
- The emitted ISO keeps the update partition entry in its partition table but
  the region it points at is zeros, so real consoles may refuse to boot it;
  emulators and USB loaders ignore the update partition.
