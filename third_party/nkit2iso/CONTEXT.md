# nkit2iso

Restores shrunk NKit v01 GameCube/Wii disc images back to full `.iso` files.

## Language

**NKit image**:
A shrunk disc image (`.nkit.iso` / `.nkit.gcz`) that keeps only data which
cannot be regenerated, plus metadata describing how to rebuild the rest.
_Avoid_: compressed ISO, rom

**Restore**:
Rebuilding the full original disc image from an NKit image.
_Avoid_: convert, unpack, decompress

**Junk**:
Nintendo's deterministic pseudo-random padding between and after files. Fully
reproducible from the game ID and disc number, so never stored.
_Avoid_: garbage, filler (filler is the inter-partition variant)

**Scrubbed region**:
An area whose original bytes were destroyed (overwritten with a constant byte)
before the image was shrunk. Preserved as-is; can never be regenerated.

**Update partition**:
The Wii system-update partition at the start of a Wii disc. Identical across
many games; some NKit images were shrunk by removing it entirely.
_Avoid_: recovery partition (that names the external file, not the partition)

**Placeholder**:
The 32 KiB stand-in NKit leaves where a removed update partition used to be.
Its first 0x100 bytes are a backup of the original partition table.

**Recovery file**:
An external file holding a removed update partition's raw bytes, identified by
the partition's CRC32 (filename ends in `_<CRC8>`). Not shipped with nkit2iso,
but publicly archived; the tool offers to download it (splicing it back makes
the restore bit-exact). Note the CRC in the name is NKit's partition CRC, not
the CRC32 of the file's own bytes — downloads are verified via the archive
index's SHA-1 and the final whole-image CRC32.

**Bit-exact restore**:
A restore whose output CRC32 matches the original redump hash stored in the
NKit header. The default and only successful outcome for images that contain
all their data.
_Avoid_: verified, perfect, 1:1

**Playable restore**:
A restore of an image with a removed update partition *without* its recovery
file: full-size, boots in emulators and USB loaders, but the update region is
zero-filled, so it is not bit-exact. Always accompanied by a warning. The
alternative — downloading the recovery file — upgrades the outcome to a
bit-exact restore.
_Avoid_: unverified ISO, scrubbed ISO
