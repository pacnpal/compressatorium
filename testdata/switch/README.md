# Switch round-trip test data (local only)

This folder is your scratch space for the real nsz round-trip test. Everything
here except this README is git-ignored, so your keys and dumps never get
committed.

## What goes where

- `keys/prod.keys` — your own `prod.keys`, dumped from a console you own (e.g.
  with Lockpick_RCM). A placeholder ships here; replace it with the real file.
- `dumps/` — drop one real `.nsp` or `.xci` you own. The test uses the first one
  it finds. The `.txt` placeholder is ignored.
- `out/` — the test writes the compressed and round-tripped files here.

## Run it

```bash
PYTHONPATH=app .venv/bin/python -m pytest tests/test_nsz_roundtrip.py -v -s
```

The test runs the real `nsz` through the app's own service code:
compress → verify (`nsz -V`) → decompress → compare SHA-256 of the original and
the round-tripped file. It **skips** (does not fail) while the placeholder keys
are in place or no real dump is present, so CI stays green.

To point at files elsewhere, override with env vars:

```bash
SWITCH_KEYS=/path/to/keysdir \
NSZ_ROUNDTRIP_DUMP=/path/to/game.nsp \
PYTHONPATH=app .venv/bin/python -m pytest tests/test_nsz_roundtrip.py -v -s
```

## Legal

Only use keys and games you own. No keys or game data are committed or shipped.
