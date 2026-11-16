# RomM swap spike (#382)

A manual check of how a real RomM treats a ROM whose file is replaced and
renamed behind its back. It is not a test and nothing runs it automatically.
The findings it produced are in `docs/ROMM_INTEGRATION_ROADMAP.md`, under
Phase 3 ("3.0 spike findings"). Re-run it when RomM changes its scan, rename
or title-id code, or before changing the swap.

## What it needs

- Docker with `docker compose` (OrbStack or Docker Desktop on a Mac).
- Python 3.10+ on the host. Standard library only.
- A Compressatorium image to convert with. The default is
  `compressatorium:smoke-latest`; build one from `latest` with
  `docker build -t compressatorium:smoke-latest .` or pass `--app-image`.
- Network access to pull `rommapp/romm:5.4.1`, `mariadb:11.8` and the Debian
  base for the fixture builder (all pinned by digest).

## Running it

```sh
python3 scripts/spike/romm_swap/spike.py --work /tmp/romm-swap-spike
```

`--work` is wiped and recreated; a non-empty folder an earlier run didn't make
is refused. Everything the run creates lives there or in the compose
project's named volumes (project `romm-swap-spike`, RomM on
`127.0.0.1:18980`, `--port` to change it). The stack is torn down with
`docker compose down -v` at the end; `--keep` leaves it up so you can look
around, and a later run tears it down first. Every password, the auth secret
and the API tokens are generated per run; the admin login is in
`WORK/.env` (`SPIKE_ADMIN_USER`, `SPIKE_ADMIN_PASSWORD`).

It takes about two minutes. Nothing touches any library but the scratch one.

## What it does

1. Builds small real-format Images in a throwaway container
   (`Dockerfile.fixtures`, `build_fixtures.py`): PS2 DVD ISOs with a
   `SYSTEM.CNF`, PSP ISOs with `UMD_DATA.BIN` and `PARAM.SFO`, GameCube discs
   with a header and FST. Each has its own serial, so RomM's title-id reader
   tells them apart.
2. Starts RomM 5.4.1 and MariaDB (`docker-compose.yml`, after RomM's own
   example) with no metadata provider keys and Hasheous off, creates the
   first admin and API tokens with chosen scopes, and runs a quick scan.
3. Gives most ROMs a save, a state, a note, a collection entry, a favourite,
   user props and a play session.
4. Runs the experiments:
   - **Swap** (`Swap PS2/GC/PSP`, `QuickSwap PS2`): convert with the app's CLI,
     stage the output in a hidden folder beside the Source,
     `PUT /api/roms/{id}` with the new `fs_name`, move the output over the
     renamed file, then a `hashes` scan by `roms_ids` (or a quick scan).
   - **Staging probes**: which staging names a quick scan indexes.
   - **Stealth**: same name, size and mtime, different bytes. Quick scan, then
     a `hashes` scan.
   - **Race**: the app writes its output beside the Source, RomM scans, then
     the Source is deleted.
   - **Re-identify** (`Reid *`, plus a plain rename as the control): the app
     converts in the library with `--delete-source`, then a quick scan.
   - **Refusals**: a rename onto an existing file, a rename whose file is
     gone, a rename to the same name, a name with a path in it.
   - **Permissions**: every call the swap makes, with tokens missing one scope
     each, and a non-admin user minting a token.
   - **Converter config**: heartbeat, `/api/config`, the task list, setting
     targets, and RomM restarted with `ROM_CONVERTO_ENABLED=true`.
5. Prints a summary.

## Evidence

- `WORK/evidence/calls.jsonl`: every request (method, path, which token,
  body) and response (status, body). Token-creation responses carry the raw
  throwaway tokens, so don't paste this file anywhere public.
- `WORK/evidence/results.json`: each experiment's database rows and files
  before and after, read from MariaDB with parameterized queries (`db_query.py`, run
  inside the RomM container).
