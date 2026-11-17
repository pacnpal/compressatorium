# rom-converto spike (#421)

A manual check of how rom-converto v0.24.0, the version RomM pins, converts
Xbox 360 ISO to ZAR, PSP PBP to ISO and 3DS CIA to and from CCI. It is not a
test and nothing runs it automatically. The findings it produced are in
`docs/VALUE_ADD_ROADMAP.md`, under "rom-converto spike findings" in step 4.
Re-run it when RomM moves its pin, or before the plugin changes a call.

## What it needs

- Python 3.10+ on the host. Standard library only.
- Docker for the two Linux targets (OrbStack or Docker Desktop). The binary
  runs in `debian:trixie-slim`, pinned by digest, the runtime base the app's
  own Dockerfile uses. `--target native` runs the macOS asset with no Docker.
- Network access to download the release asset and one homebrew CIA. Both are
  checked against SHA-256 pins in the script, and the run stops on a mismatch.

## Running it

```sh
python3 scripts/spike/rom_converto/spike.py --work /tmp/rom-converto-spike
python3 scripts/spike/rom_converto/spike.py --work /tmp/rom-converto-spike --target docker-amd64
python3 scripts/spike/rom_converto/spike.py --work /tmp/rom-converto-spike --target native
```

`--work` must be new or empty, or a folder an earlier run made (it holds a
`.rom-converto-spike` marker). Each target runs in its own `run-<target>`
folder, which is recreated on every run. A run takes under a minute and prints
one PASS, FAIL or NOTE line per check, then writes `results-<target>.json`.
A NOTE is something the script could not prove and says so.

## What it does

1. Downloads the pinned asset (`rom-converto-cli-linux-arm64`,
   `rom-converto-cli-linux-x64`, or the macOS one) and checks its SHA-256.
2. **Xbox 360** (`fixtures.py` builds the inputs): a hand-built XDVDFS image of
   three files of random bytes, at the three partition bases rom-converto
   probes (a trimmed ISO, XGD3 and XGD2). It runs `xenon compress`, `verify`
   and `extract`, compares every extracted file with the source bytes, checks
   that no command turns a ZAR back into an ISO, and tries the overwrite,
   `-f`, bad-input and `-q` cases.
3. **PSP**: a hand-made EBOOT.PBP with a valid PARAM.SFO and a junk PSAR. It
   checks `psp info`, and that `psp to-iso` refuses the file and leaves no
   output. A working PBP needs a game's encrypted image, so the success path
   is not tested.
4. **3DS**: FBI 2.6.1 (MIT-licensed homebrew by Steveice10) is the only real
   container. It runs CIA to CCI (padded and `--trim`), CCI back to CIA,
   compares the NCCH bytes across all three, and feeds it junk, truncated and
   missing inputs to record the messages and exit codes.
5. Records exit codes, which stream the output goes to, and that a conversion
   works with `--network none` and no key files.

## What it does not use

No commercial game image, key or `seeddb.bin` is downloaded, built or read.
Every input is seeded random bytes, a hand-made header, or the MIT homebrew
CIA. The binary carries its own common keys; the script never supplies any.
