# nkit2iso

Convert **GameCube and Wii** `*.nkit.iso` (and GCZ-compressed `*.nkit.gcz`)
disc images back to plain, bit-exact `.iso` files that emulators (Dolphin,
Nintendont, …) can boot.

`nkit2iso` is a single, dependency-free static binary written in Go. It restores
the shrunk NKit v01 format by replaying the preserved data and regenerating the
removed "junk" padding — and, for Wii, the AES encryption and the H0–H3 hash
tree — then verifies the result against the original CRC32 stored inside the
NKit header, so a successful run is **redump-verified 1:1**.

```
$ nkit2iso -i "Mario Kart - Double Dash!! (USA).nkit.iso"
Restoring Mario Kart - Double Dash!! (USA).nkit.iso -> Mario Kart - Double Dash!! (USA).iso
  100%
CRC32 099E2C6D  MATCH (redump-verified)
```

> **GameCube and Wii are both supported and byte-exact** (GameCube and Wii
> single/multi-partition discs, including scrubbed dumps). Input may be a plain
> `.nkit.iso` or a GCZ-compressed `.nkit.gcz`, which is transparently
> decompressed. The one case that can't be restored byte-exact from the
> `.nkit.iso` alone is a Wii image whose **update partition was removed** —
> that data isn't in the file. For these images `nkit2iso` offers to download
> the matching, publicly archived recovery file (the exact URL is shown) and
> splice it back in for a bit-exact, redump-verified restore — or to convert
> without it into a full-size, playable ISO (the missing region is
> zero-filled, exactly like official NKit does without recovery files), with a
> clear warning that the result is not redump-verifiable.
> See [Removed update partitions](#removed-wii-update-partitions).

## Install

### Homebrew (macOS / Linux)

```sh
brew install DonMikone/tap/nkit2iso
```

Installs the latest release; `brew` also clears the macOS quarantine flag for
you, so no extra step is needed.

### Manual download

Download the archive for your platform from the
[Releases](https://github.com/DonMikone/nkit2iso/releases) page and unpack
the `nkit2iso` binary somewhere on your `PATH`.

| Platform | Asset |
|----------|-------|
| Windows (x64) | `nkit2iso_<ver>_windows_amd64.zip` |
| Linux (x64) | `nkit2iso_<ver>_linux_amd64.tar.gz` |
| macOS (Intel + Apple Silicon) | `nkit2iso_<ver>_macos_universal.tar.gz` |

### macOS: clear the quarantine flag

The macOS binary is **not code-signed** (that needs a paid Apple Developer
account). After unpacking, remove the quarantine attribute once:

```sh
xattr -dr com.apple.quarantine ./nkit2iso
```

Otherwise Gatekeeper will refuse to run it. This is expected for open-source
CLI tools distributed outside the App Store.

## Usage

```
nkit2iso -i <in.nkit.iso> [-o <out.iso>] [-f] [-recovery ask|download|none]

  -i   input .nkit.iso file (may also be given as a positional argument)
  -o   output .iso file (default: input name with a .iso extension)
  -f   overwrite the output file if it already exists
  -recovery   what to do when a Wii image's update partition was removed:
              ask (default: prompt on a terminal), download (fetch the
              recovery file without asking), none (always zero-fill)
  -version   print version and exit
```

Examples:

```sh
# Explicit output
nkit2iso -i game.nkit.iso -o game.iso

# Default output (game.nkit.iso -> game.iso)
nkit2iso -i game.nkit.iso

# Positional input
nkit2iso game.nkit.iso

# GCZ-compressed input (decompressed on the fly)
nkit2iso game.nkit.gcz          # -> game.iso
```

The exit code is `0` only when the reconstructed ISO's CRC32 matches the value
stored in the NKit header. Any mismatch or error exits non-zero and the
half-written output is removed. The one exception is a Wii image whose update
partition was removed at shrink time *and* restored without its recovery file:
the CRC check is skipped (it cannot match), a warning is printed, and the
playable ISO is kept with exit code `0`.

## How it works

If the input is a `.gcz` container (Dolphin's block-compressed format, as
produced by `*.nkit.gcz`), `nkit2iso` first inflates it on the fly with the
standard-library zlib — one block at a time, constant memory — and feeds the
resulting nkit stream straight into the restore below.

An NKit v01 GameCube image is a normal disc image with all reproducible data
removed to shrink it:

- **Junk padding** — the pseudo-random filler Nintendo writes between and after
  files. It is fully determined by the 4-byte game ID and disc number, so
  `nkit2iso` regenerates it exactly rather than storing it.
- **Gaps** — inter-file gaps are run-length encoded; any non-reproducible bytes
  (scrubbed regions, partial junk) are preserved inline.
- **All-junk files** — files whose entire contents are junk are dropped from the
  image and rebuilt on restore.

Restoration parses the file system table (FST), streams each preserved file back
into place, regenerates junk and gaps to rebuild the original disc layout, and
rewrites the FST/header offsets to their original values. The whole image is
streamed with constant memory, then CRC32-checked against the header.

For **Wii**, each partition's filesystem is rebuilt the same way in *decrypted*
space, then `nkit2iso` regenerates the H0–H3 SHA-1 hash tree, re-encrypts every
0x8000 cluster with the partition's AES title key, restores scrubbed regions, and
reconstructs the partition table. Only stdlib crypto (`crypto/aes`, `crypto/sha1`)
is used — still zero external dependencies.

## Removed Wii update partitions

Some Wii `.nkit.iso` files were shrunk by dropping the update
(system-menu/IOS) partition, whose data is not stored in the file and cannot
be regenerated. When `nkit2iso` meets one of these it offers two ways to
restore it (interactively on a terminal, or preselected via `-recovery`):

1. **Download the recovery file** (`-recovery download`). The removed
   partition's data is shared between many games and is archived publicly in
   the "NKit Recovery Partitions" collection on archive.org. `nkit2iso` looks
   the file up by the CRC32 stored in the NKit header, prints the exact index
   and download URLs for full transparency, verifies the download against the
   archive's SHA-1, and splices it back in. The result is **bit-exact** and
   passes the normal redump CRC32 check. Nothing is fetched without the
   prompt being answered or `-recovery download` being given explicitly, and
   this is the only situation in which `nkit2iso` touches the network.
2. **Restore without it** (`-recovery none`, or when the download fails or
   stdin is not a terminal). The original partition table (which NKit backs
   up inside the image) is put back and the update partition's region is
   zero-filled — the same thing official NKit does when its recovery files
   are missing. The result boots in Dolphin and USB loaders, but is not
   redump-verifiable and may not boot on an unmodified console, so a warning
   is printed and the final CRC32 check is skipped.

## Build from source

Requires Go 1.24+.

```sh
git clone https://github.com/DonMikone/nkit2iso
cd nkit2iso
go build -o nkit2iso .
go test ./...     # junk-PRNG, Wii common-key and hash-tree self-checks
```

## Credits & license

The NKit format and its GameCube/Wii algorithms were created by **Nanook**
([Nanook/NKitv1](https://github.com/Nanook/NKitv1)). `nkit2iso` is an independent,
clean-room Go reimplementation of the *algorithms* (no source code was copied)
built for cross-platform, dependency-free restoration.

Licensed under the [MIT License](LICENSE). This tool converts disc images you
already own; it does not include or distribute any game data.
