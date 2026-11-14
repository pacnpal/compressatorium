# Scope: image bytes and the things that point at them

Compressatorium changes or checks the bytes of game images, and keeps
Referrers (RomM, playlists, frontend gamelists) pointing at the result. It does
not catalogue what a collection is missing. We drew the line here because the
value-add roadmap was growing the app into a general library manager one
reasonable feature at a time.

In scope: converting, checking that an Image is Intact, health scrub, duplicate
finder, rename to DAT name, patching, and an Inbox that sorts incoming
files into a Library. Each one reads or writes image bytes, or renames an Image and fixes its
Referrers.

Out of scope: set completion ("812 of 1,034"), missing-game lists, and "best
version of each game". The DAT data to build them is already imported, so this
will look like an obvious feature to add. It was left out on purpose. It never
touches a file, it is what RomVault and clrmamepro already do, and a list of
missing games pulls the product toward "where do I get them".

Also out of scope: serving files to devices. RomM does that.
