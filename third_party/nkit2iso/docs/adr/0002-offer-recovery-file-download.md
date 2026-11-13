# Offer to download recovery files for removed Wii update partitions

Amends [0001](0001-zero-fill-removed-update-partitions.md): zero-filling is no
longer the only outcome. When an image's update partition was removed, the
user is now always offered a choice — download the matching recovery file from
the public archive.org mirror (item `MarioCubeLite`) for a bit-exact restore,
or restore without it (the ADR 0001 zero-fill behaviour). On a terminal the
choice is an interactive prompt; scripts preselect it with
`-recovery download|none` (`-recovery ask` is the default). For transparency
the tool prints the exact index URL and file URL before touching the network;
this is the only code path that goes online.

The recovery file is located by the CRC32 at header 0x218, which archive.org's
filenames end in (`_<CRC8>`). That CRC is NKit's partition CRC, *not* the
CRC32 of the file's own bytes, so the download is verified against the archive
index's SHA-1 and — decisively — by the normal whole-image redump CRC32 check,
which a spliced restore must now pass like any other.

## Considered options

- **Prompt + flag (chosen).** Keeps the default interactive experience honest
  (no silent network access, no silent non-verifiable output) and stays
  scriptable.
- **Always download silently.** Rejected: a CLI that phones home unannounced,
  and large downloads (up to ~200 MiB) the user didn't ask for.
- **Keep zero-fill only (ADR 0001).** Rejected: the splice was already proven
  byte-perfect, and a bit-exact ISO is strictly better when the user opts in.

## Consequences

- The tool is no longer strictly offline; it contacts archive.org only in
  this one path and only after explicit consent (prompt answer or
  `-recovery download`).
- Download failures (or archive gaps) fall back to the zero-fill restore with
  a warning, so a restore never gets worse than ADR 0001 behaviour.
- Downloads stream to a temp file next to the output, are SHA-1-verified, and
  are deleted after splicing.
- With stdin not a terminal, `-recovery ask` behaves like `none` plus a hint,
  so pipelines never hang on a prompt.
