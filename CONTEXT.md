# Compressatorium

Converts and checks game images, and keeps the other tools that point at those
images working afterwards. It changes or reads the bytes of an image. It does
not catalogue what a collection is missing.

## Language

### What gets converted

**Image**:
One convertible unit: a disc, a cartridge dump or a PS3 game folder, made of one or more files. Rules, checks and the ledger work on Images.
_Avoid_: ROM, file, game, dump

**Title**:
One game as a person thinks of it, made of one or more Images (a four-disc game is one Title).
_Avoid_: Game, set, entry

**ROM**:
RomM's own record for a game. Used only when talking about RomM's data.
_Avoid_: Using "ROM" for an Image or a file

**Platform**:
The console an Image belongs to, either reported by RomM or worked out from the Image itself.
_Avoid_: System, console, core

**Unsorted**:
An Image whose Platform could not be worked out. No rule touches it.
_Avoid_: Unknown, misc

**Finding**:
Something the app noticed about an Image that needs a person to decide: it is not Intact, its Platform disagrees with its folder, it duplicates another Image.
_Avoid_: Error, warning, issue, alert

### Where images live

**Volume**:
A directory mounted into the app, the outer boundary of everything it may read or write.
_Avoid_: Mount, share, drive

**Library**:
A set of Images that rules run against. It is either a RomM library or a folder library (one root folder the user registers), and it always sits inside a Volume.
_Avoid_: Catalog, catalog source, collection

**Inbox**:
A watched folder that feeds one Library. Images dropped in it are converted into the Library, and the originals are moved aside, never deleted.
_Avoid_: Drop zone, watch folder, import folder

**Rule**:
A standing instruction to convert or check the Images of one Platform in one Library.
_Avoid_: Automation, job, task, schedule

**Referrer**:
Anything that points at an Image by its path: RomM's ROM, an `.m3u` playlist, a frontend gamelist. Referrers are updated when a Source is replaced.
_Avoid_: Frontend, sidecar, integration

**Source**:
The Image a conversion reads. It is the only thing the app ever deletes on its own, and only after its output is Round-tripped and every Referrer is updated.
_Avoid_: Using "source" for where a list of images comes from (that is a Library)

**Ledger**:
The permanent record of finished conversions: what was read, what was written, what was deleted and which Referrers were updated.
_Avoid_: History, job log, audit log

### What a check proves

**Intact**:
The file passed its own tool's container check, so it reads back without errors.
_Avoid_: Verified, valid, OK

**Round-tripped**:
At conversion time, the content decoded from the output equalled the content decoded from its Source. It is a claim about image content, never about container bytes.
_Avoid_: Verified, lossless-checked

**Recognized**:
The image's hash was found in a DAT or in Hasheous, so it is a known dump.
_Avoid_: Verified, matched, identified (RomM uses "verified" for this)

**Verify**:
The action that checks whether a file is Intact. The bare word "verified" is never used as a state.
