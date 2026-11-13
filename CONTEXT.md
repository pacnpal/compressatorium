# Compressatorium

Converts and checks game images, and keeps the other tools that point at those
images working afterwards. It changes or reads the bytes of an image. It does
not catalogue what a collection is missing.

## Language

### Where images live

**Volume**:
A directory mounted into the app, the outer boundary of everything it may read or write.
_Avoid_: Mount, share, drive

**Library**:
A set of game images that rules run against. It is either a RomM library or a folder library, and it always sits inside a Volume.
_Avoid_: Catalog, catalog source, collection

**Source**:
The input file of one conversion.
_Avoid_: Using "source" for where a list of images comes from (that is a Library)

### What a check proves

**Intact**:
The file passed its own tool's container check, so it reads back without errors.
_Avoid_: Verified, valid, OK

**Round-tripped**:
At conversion time, the output decoded back to the same content as its Source.
_Avoid_: Verified, lossless-checked

**Recognized**:
The image's hash was found in a DAT or in Hasheous, so it is a known dump.
_Avoid_: Verified, matched, identified (RomM uses "verified" for this)

**Verify**:
The action that checks whether a file is Intact. The bare word "verified" is never used as a state.
