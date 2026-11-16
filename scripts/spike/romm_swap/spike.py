#!/usr/bin/env python3
"""RomM out-of-band swap spike (#382): how a real RomM treats a ROM whose file is
replaced and renamed behind its back.

Runs the official RomM image (pinned in docker-compose.yml) on a scratch
library of small real-format Images, converted by Compressatorium's own image,
and records every HTTP call and the database state around it. Standard library
only; needs Docker (with `docker compose`) and nothing else on the host.

    python3 scripts/spike/romm_swap/spike.py --work /some/scratch/dir

Writes WORK/evidence/calls.jsonl (every request and response) and
WORK/evidence/results.json (each experiment's before and after state), prints
a summary, and tears the stack down unless --keep is given. See README.md.
"""
import argparse
import base64
import datetime as dt
import hashlib
import http.client
import json
import os
import secrets
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJECT = "romm-swap-spike"
FIXTURE_IMAGE = "compressatorium-romm-spike-fixtures:latest"
ADMIN_USER = "spikeadmin"
# The scopes the swap needs (see the permissions experiment): read the ROM,
# rename it, queue and follow the rehash.
SWAP_SCOPES = ["roms.read", "roms.write", "tasks.run"]
# Where the swap stages its outputs: a dot-prefixed folder beside the Source.
STAGE_DIR = ".compressatorium-stage"

# Experiment E1 (rename + hashes scan) and E2 (rename + quick scan): stem,
# platform, target format.
SWAPS = [
    ("Swap PS2", "ps2", "chd", "hashes"),
    ("Swap GC", "ngc", "rvz", "hashes"),
    ("Swap PSP", "psp", "cso", "hashes"),
    ("QuickSwap PS2", "ps2", "chd", "quick"),
]
# Experiment E4: converted by the app with --delete-source, then a quick scan.
REIDS = [
    ("Reid PS2 CHD", "ps2", "chd"),
    ("Reid PS2 CSO", "ps2", "cso"),
    ("Reid PS2 ZSO", "ps2", "zso"),
    ("Reid GC RVZ", "ngc", "rvz"),
    ("Reid GC WIA", "ngc", "wia"),
    ("Reid GC GCZ", "ngc", "gcz"),
    ("Reid PSP CSO", "psp", "cso"),
    ("Reid PSP ZSO", "psp", "zso"),
    ("Reid PSP CHD", "psp", "chd"),
]
# ROMs that get a save, a state, a note, a collection, a favourite, user props
# and a play session before anything is done to them.
WITH_USER_DATA = (
    [s[0] for s in SWAPS] + [r[0] for r in REIDS]
    + ["Rename PS2", "Refuse PS2", "Race PS2 CHD"]
)


class Spike:
    def __init__(self, work, port, app_image):
        self.work = work
        self.port = port
        self.app_image = app_image
        # The admin's password is made per run and kept in WORK/.env.
        self.admin = (ADMIN_USER, secrets.token_urlsafe(18))
        self.evidence = work / "evidence"
        self.library = work / "library" / "roms"
        self.calls = None
        self.results = {}
        self.tokens = {}
        self.ids = {}

    # ---- plumbing -------------------------------------------------------

    def compose(self, *args, check=True, stdin=None):
        return docker("compose", "-p", PROJECT, "-f", str(HERE / "docker-compose.yml"),
                      "--env-file", str(self.work / ".env"), *args, check=check, stdin=stdin)

    def sql(self, query, *params):
        """Rows of a parameterized query (`?` placeholders) against RomM's
        database, as dicts of strings. db_query.py runs it inside the RomM
        container with RomM's own connector."""
        request = json.dumps({"query": query, "params": list(params)})
        out = self.compose("exec", "-T", "romm", "/src/.venv/bin/python", "-", request,
                           stdin=(HERE / "db_query.py").read_text(encoding="utf-8")).stdout
        return json.loads(out)

    def log_call(self, entry):
        with open(self.calls, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")

    def http(self, method, path, who=None, json_body=None, form=None, files=None,
             basic=None, label="", headers=None):
        """One request to the local RomM, logged with its response. `who` names
        the API token to send. Returns (status, parsed body)."""
        headers = dict(headers or {})
        data = None
        logged_body = None
        if who:
            headers["Authorization"] = f"Bearer {self.tokens[who]}"
        if basic:
            raw = base64.b64encode(f"{basic[0]}:{basic[1]}".encode()).decode()
            headers["Authorization"] = f"Basic {raw}"
        if json_body is not None:
            data = json.dumps(json_body).encode()
            headers["Content-Type"] = "application/json"
            logged_body = {k: "(redacted)" if k == "password" else v
                           for k, v in json_body.items()}
        elif form is not None or files is not None:
            boundary = uuid.uuid4().hex
            parts = []
            for key, value in (form or {}).items():
                parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"'
                             f"\r\n\r\n{value}\r\n".encode())
            for key, (name, content) in (files or {}).items():
                parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"; '
                             f'filename="{name}"\r\nContent-Type: application/octet-stream'
                             f"\r\n\r\n".encode() + content + b"\r\n")
            data = b"".join(parts) + f"--{boundary}--\r\n".encode()
            headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
            logged_body = {"form": form or {},
                           "files": {k: f"{v[0]} ({len(v[1])} bytes)"
                                     for k, v in (files or {}).items()}}
        status, raw, _ = local_request(self.port, method, path, data, headers)
        try:
            body = json.loads(raw) if raw else None
        except ValueError:
            body = raw.decode(errors="replace")
        self.log_call({"label": label, "method": method, "path": path,
                       "auth": who or ("basic" if basic else None),
                       "request": logged_body, "status": status, "response": body})
        return status, body

    def wait_task(self, task_id, who):
        for _ in range(240):
            status, body = self.http("GET", f"/api/tasks/{task_id}", who=who,
                                     label="poll task")
            if status == 200 and body.get("status") in ("finished", "failed", "stopped",
                                                          "canceled"):
                return body
            time.sleep(1)
        raise RuntimeError(f"task {task_id} did not finish")

    def scan(self, payload, who="full", label="scan"):
        status, body = self.http("POST", "/api/tasks/scan", who=who, json_body=payload,
                                 label=label)
        if status != 202:
            raise RuntimeError(f"scan refused: {status} {body}")
        final = self.wait_task(body["task_id"], who)
        return {"request": payload, "queued": body, "final": final}

    def convert(self, mount, paths, fmt, extra=()):
        """Convert with Compressatorium's own CLI, `mount` at /data/games."""
        app_config = self.work / "app-config"
        app_config.mkdir(exist_ok=True)
        out = docker("run", "--rm", "-e", "COMPRESSATORIUM_MOUNT_ROOT=/data",
                     "-v", f"{mount}:/data/games", "-v", f"{app_config}:/config",
                     "--entrypoint", "compressatorium", self.app_image,
                     "convert", "--to", fmt, "--json", *extra, *paths, check=False)
        rows = [json.loads(line) for line in out.stdout.splitlines() if line.startswith("{")]
        if out.returncode != 0 or not rows or any(r["status"] != "completed" for r in rows):
            raise RuntimeError(f"conversion failed: {out.stdout}\n{out.stderr[-2000:]}")
        return rows

    # ---- state snapshots --------------------------------------------------

    def rom_row(self, rom_id):
        rows = self.sql(
            "SELECT id, name, fs_name, fs_path, fs_size_bytes, crc_hash, md5_hash, sha1_hash, "
            "title_id, missing_from_fs, updated_at FROM roms WHERE id = ?", int(rom_id))
        return rows[0] if rows else None

    def file_rows(self, rom_id):
        return self.sql(
            "SELECT id, file_name, file_size_bytes, last_modified, sha1_hash, chd_sha1_hash, "
            "title_id, missing_from_fs FROM rom_files WHERE rom_id = ?", int(rom_id))

    def user_data(self, rom_id):
        rid = int(rom_id)
        return {
            "saves": self.sql("SELECT id, file_name, file_path FROM saves WHERE rom_id = ?", rid),
            "states": self.sql("SELECT id, file_name, file_path FROM states WHERE rom_id = ?",
                               rid),
            "notes": self.sql("SELECT id, title FROM rom_notes WHERE rom_id = ?", rid),
            "collections": self.sql(
                "SELECT c.id, c.name, c.is_favorite FROM collections_roms cr JOIN collections c "
                "ON c.id = cr.collection_id WHERE cr.rom_id = ?", rid),
            "rom_user": self.sql(
                "SELECT rating, status, backlogged FROM rom_user WHERE rom_id = ?", rid),
            "play_sessions": self.sql(
                "SELECT id, duration_ms FROM play_sessions WHERE rom_id = ?", rid),
        }

    def snapshot(self, rom_id):
        return {"rom": self.rom_row(rom_id), "files": self.file_rows(rom_id),
                "user_data": self.user_data(rom_id)}

    def roms_named(self, stem):
        """Every ROM row whose file name starts with `stem` (old and new)."""
        return self.sql(
            "SELECT id, fs_name, fs_size_bytes, sha1_hash, title_id, missing_from_fs "
            "FROM roms WHERE fs_name LIKE ? ORDER BY id", stem + "%")

    @staticmethod
    def sha1(path):
        # A checksum to compare with the one RomM stores, not a security use.
        h = hashlib.new("sha1", usedforsecurity=False)
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()

    # ---- setup ----------------------------------------------------------

    def setup(self):
        if self.work.exists():
            # Only a directory an earlier run made (or an empty one) is wiped.
            if any(self.work.iterdir()) and not (self.work / ".env").exists():
                raise SystemExit(f"{self.work} is not empty and not a spike work dir")
            self.teardown(quiet=True)
            shutil.rmtree(self.work)
        for sub in ("evidence", "fixtures", "stage", "assets", "config", "app-config"):
            (self.work / sub).mkdir(parents=True)
        (self.work / "config" / "config.yml").write_text("", encoding="utf-8")
        # Every secret is made here, per run, and lives only in WORK/.env.
        (self.work / ".env").write_text(
            f"SPIKE_WORK={self.work}\nSPIKE_PORT={self.port}\n"
            f"SPIKE_DB_PASSWORD={secrets.token_urlsafe(18)}\n"
            f"SPIKE_DB_ROOT_PASSWORD={secrets.token_urlsafe(18)}\n"
            f"SPIKE_AUTH_SECRET={secrets.token_hex(32)}\n"
            f"SPIKE_ADMIN_USER={self.admin[0]}\nSPIKE_ADMIN_PASSWORD={self.admin[1]}\n"
            "ROM_CONVERTO_ENABLED=false\n", encoding="utf-8")
        self.calls = self.evidence / "calls.jsonl"

        print("building fixtures ...")
        docker("build", "-q", "-t", FIXTURE_IMAGE, "-f", str(HERE / "Dockerfile.fixtures"),
               str(HERE))
        docker("run", "--rm", "-v", f"{HERE / 'build_fixtures.py'}:/build_fixtures.py:ro",
               "-v", f"{self.work / 'fixtures'}:/out", FIXTURE_IMAGE,
               "python3", "/build_fixtures.py", "/out")
        shutil.copytree(self.work / "fixtures", self.library)

        print("starting RomM ...")
        self.compose("up", "-d")
        self.wait_heartbeat()
        self.bootstrap_auth()

    def wait_heartbeat(self):
        for _ in range(120):
            try:
                if local_request(self.port, "GET", "/api/heartbeat", timeout=5)[0] == 200:
                    return
            except (http.client.HTTPException, OSError):
                pass
            time.sleep(2)
        raise RuntimeError("RomM did not come up")

    def bootstrap_auth(self):
        # The first admin is created without auth, behind RomM's CSRF check:
        # the heartbeat sets the cookie, the header echoes it.
        _, _, cookies = local_request(self.port, "GET", "/api/heartbeat")
        csrf = cookies["romm_csrftoken"]
        status, body = self.http(
            "POST", "/api/users", label="create first admin",
            json_body={"username": self.admin[0], "email": "spike@example.invalid",
                       "password": self.admin[1], "role": "admin"},
            headers={"X-CSRFToken": csrf, "Cookie": f"romm_csrftoken={csrf}"})
        if status != 201:
            raise RuntimeError(f"could not create the admin: {status} {body}")
        scopes_of = {
            "full": None,
            "swap": SWAP_SCOPES,
            "no_roms_write": ["roms.read", "tasks.run"],
            "no_tasks_run": ["roms.read", "roms.write"],
            "no_roms_read": ["roms.write", "tasks.run"],
            "read_only": ["roms.read", "platforms.read"],
        }
        _, me = self.http("GET", "/api/users/me", basic=self.admin, label="admin scopes")
        for name, scopes in scopes_of.items():
            status, body = self.http(
                "POST", "/api/client-tokens", basic=self.admin,
                json_body={"name": f"spike-{name}", "scopes": scopes or me["oauth_scopes"]},
                label=f"create token {name}")
            if status != 201:
                raise RuntimeError(f"could not create token {name}: {status} {body}")
            self.tokens[name] = body["raw_token"]

    def teardown(self, quiet=False):
        if not (self.work / ".env").exists():
            return
        res = self.compose("down", "-v", "--remove-orphans", check=False)
        if not quiet:
            print(res.stderr.strip()[-400:])

    # ---- experiments ----------------------------------------------------

    def initial_scan(self):
        print("initial quick scan ...")
        result = self.scan({"type": "quick"}, label="initial quick scan")
        rows = self.sql("SELECT id, fs_name, fs_path, fs_size_bytes, sha1_hash, title_id "
                        "FROM roms ORDER BY id")
        for row in rows:
            self.ids[row["fs_name"].rsplit(".", 1)[0]] = int(row["id"])
        self.results["initial_scan"] = {"scan": result, "roms": rows}

    def seed_user_data(self):
        print("seeding user data ...")
        def ok(result, *expected):
            status, body = result
            if status not in expected:
                raise RuntimeError(f"seeding failed: {status} {body}")
            return body

        coll = ok(self.http("POST", "/api/collections", who="full",
                            form={"name": "Spike collection"}, label="create collection"),
                  200, 201)
        fav = ok(self.http("POST", "/api/collections?is_favorite=true", who="full",
                           form={"name": "Favourites"}, label="create favourites"), 200, 201)
        ids = [self.ids[stem] for stem in WITH_USER_DATA]
        ok(self.http("POST", f"/api/collections/{coll['id']}/roms", who="full",
                     json_body={"rom_ids": ids}, label="add to collection"), 200)
        ok(self.http("POST", f"/api/collections/{fav['id']}/roms", who="full",
                     json_body={"rom_ids": ids}, label="add to favourites"), 200)
        now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
        for stem in WITH_USER_DATA:
            rid = self.ids[stem]
            ok(self.http("POST", f"/api/saves?rom_id={rid}&emulator=pcsx2", who="full",
                         files={"saveFile": (f"{stem}.sav", b"SAVE" + stem.encode())},
                         label=f"save for {stem}"), 200, 201)
            ok(self.http("POST", f"/api/states?rom_id={rid}&emulator=pcsx2", who="full",
                         files={"stateFile": (f"{stem}.state", b"STATE" + stem.encode())},
                         label=f"state for {stem}"), 200, 201)
            ok(self.http("POST", f"/api/roms/{rid}/notes", who="full",
                         json_body={"title": f"note on {stem}", "content": "kept?"},
                         label=f"note for {stem}"), 200, 201)
            ok(self.http("PUT", f"/api/roms/{rid}/props", who="full",
                         json_body={"rating": 7, "backlogged": True, "status": "incomplete"},
                         label=f"props for {stem}"), 200)
            start = now - dt.timedelta(hours=1)
            ok(self.http("POST", "/api/play-sessions", who="full",
                         json_body={"sessions": [{"rom_id": rid,
                                                  "start_time": start.isoformat(),
                                                  "end_time": now.isoformat(),
                                                  "duration_ms": 3_600_000}]},
                         label=f"play session for {stem}"), 200, 201)

    def stage_swaps(self):
        """Convert each swap ROM outside the library, then stage it in a hidden
        folder beside the Source. Probe which staging names a scan indexes."""
        print("converting swap outputs ...")
        src = self.work / "swapsrc"
        for stem, platform, fmt, _ in SWAPS:
            folder = src / stem
            folder.mkdir(parents=True)
            shutil.copy2(self.library / platform / f"{stem}.iso", folder)
            row = self.convert(folder, [f"/data/games/{stem}.iso"], fmt)[0]
            out = folder / f"{stem}.{fmt}"
            staged = self.library / platform / STAGE_DIR / f"{stem}.{fmt}"
            staged.parent.mkdir(exist_ok=True)
            shutil.copy2(out, staged)
            self.results.setdefault("conversions", []).append(
                {"stem": stem, "app_cli": row,
                 "staged_as": str(staged.relative_to(self.library)),
                 "sha1": self.sha1(staged), "size": staged.stat().st_size})
        # Two other staging names, as files beside the Source: a dot-prefixed
        # file and one with an extension RomM excludes by default.
        probe_src = src / "Swap PS2" / "Swap PS2.chd"
        probes = [".Probe PS2.chd.stage", "Probe PS2.chd.tmp"]
        for name in probes:
            shutil.copy2(probe_src, self.library / "ps2" / name)
        count_before = self.sql("SELECT count(*) AS n FROM roms")[0]["n"]
        scan = self.scan({"type": "quick"}, label="quick scan with staged outputs")
        count_after = self.sql("SELECT count(*) AS n FROM roms")[0]["n"]
        names = {f"{stem}.{fmt}" for stem, _, fmt, _ in SWAPS} | set(probes)
        rows = [r for r in self.sql("SELECT id, fs_name, fs_path FROM roms")
                if r["fs_name"] in names]
        for name in probes:
            (self.library / "ps2" / name).unlink()
        self.results["staging"] = {
            "hidden_folder": STAGE_DIR, "file_probes": probes,
            "roms_before": count_before, "roms_after": count_after,
            "rows_for_staged_names": rows, "scan": scan["final"]}

    def swap(self, stem, platform, fmt, rescan):
        """E1/E2: rename the record in RomM, move the output over, rescan."""
        rid = self.ids[stem]
        folder = self.library / platform
        staged = folder / STAGE_DIR / f"{stem}.{fmt}"
        final_name = f"{stem}.{fmt}"
        iso_sha1 = self.sha1(folder / f"{stem}.iso")
        before = self.snapshot(rid)
        status, body = self.http("PUT", f"/api/roms/{rid}", who="swap",
                                 form={"fs_name": final_name}, label=f"rename {stem}")
        after_put = self.snapshot(rid)
        disk_after_put = {
            "iso_exists": (folder / f"{stem}.iso").exists(),
            "final_exists": (folder / final_name).exists(),
            "final_sha1_is_iso": (folder / final_name).exists()
            and self.sha1(folder / final_name) == iso_sha1,
        }
        os.replace(staged, folder / final_name)
        if rescan == "hashes":
            scan = self.scan({"type": "hashes", "roms_ids": [rid], "apis": []}, who="swap",
                             label=f"hashes scan {stem}")
        else:
            scan = self.scan({"type": "quick"}, who="swap", label=f"quick scan {stem}")
        after = self.snapshot(rid)
        _, api_rom = self.http("GET", f"/api/roms/{rid}", who="swap",
                               label=f"read back {stem}")
        self.results.setdefault("swaps", []).append({
            "stem": stem, "format": fmt, "rescan": rescan, "rom_id": rid,
            "put": {"status": status,
                    "fs_name": body.get("fs_name") if isinstance(body, dict) else body},
            "disk_after_put": disk_after_put,
            "new_file_sha1": self.sha1(folder / final_name),
            "new_file_size": (folder / final_name).stat().st_size,
            "scan": scan, "before": before, "after_put": after_put, "after": after,
            "api_after": {k: api_rom.get(k) for k in (
                "id", "fs_name", "fs_size_bytes", "sha1_hash", "title_id", "missing_from_fs")},
            "rows_for_stem": self.roms_named(stem),
        })

    def stability(self):
        """A library quick scan after every swap: no new rows, no ghosts."""
        scan = self.scan({"type": "quick"}, label="quick scan after all swaps")
        self.results["after_swaps_quick_scan"] = {
            "scan": scan["final"],
            "rows": {stem: self.roms_named(stem) for stem, _, _, _ in SWAPS},
            "missing": self.sql("SELECT id, fs_name FROM roms WHERE missing_from_fs = 1"),
        }

    def stealth(self):
        """E2b: same name, same size, same mtime, other bytes. Quick, then hashes."""
        stem = "Stealth PS2"
        rid = self.ids[stem]
        path = self.library / "ps2" / f"{stem}.iso"
        st = path.stat()
        before = self.snapshot(rid)
        shutil.copyfile(self.work / "fixtures" / "ps2" / "Orphan PS2.iso", path)
        os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))
        new_sha1 = self.sha1(path)
        quick = self.scan({"type": "quick"}, label="quick scan after stealth swap")
        after_quick = self.snapshot(rid)
        hashes = self.scan({"type": "hashes", "roms_ids": [rid], "apis": []}, who="swap",
                           label="hashes scan after stealth swap")
        after_hashes = self.snapshot(rid)
        self.results["stealth"] = {
            "rom_id": rid, "old_sha1": before["rom"]["sha1_hash"], "new_sha1": new_sha1,
            "size_unchanged": path.stat().st_size == st.st_size,
            "mtime_unchanged": path.stat().st_mtime_ns == st.st_mtime_ns,
            "before": before, "after_quick": after_quick, "after_hashes": after_hashes,
            "quick": quick["final"], "hashes": hashes["final"]}

    def race(self):
        """E4b: RomM scans while the output and the Source are both on disk."""
        stem = "Race PS2 CHD"
        rid = self.ids[stem]
        folder = self.library / "ps2"
        row = self.convert(folder, [f"/data/games/{stem}.iso"], "chd")[0]
        first = self.scan({"type": "quick"}, label="quick scan with output beside Source")
        rows_between = self.roms_named(stem)
        (folder / f"{stem}.iso").unlink()
        second = self.scan({"type": "quick"}, label="quick scan after Source deleted")
        self.results["race"] = {
            "rom_id": rid, "app_cli": row, "rows_between": rows_between,
            "rows_after": self.roms_named(stem),
            "old_row_after": self.snapshot(rid),
            "scans": [first["final"], second["final"]]}

    def reidentify(self):
        """E4: the app converts in the library and deletes the Source; RomM quick-scans."""
        print("converting in the library with --delete-source ...")
        before = {stem: self.snapshot(self.ids[stem]) for stem, _, _ in REIDS}
        rename_before = self.snapshot(self.ids["Rename PS2"])
        rows = []
        for stem, platform, fmt in REIDS:
            rows += self.convert(self.library / platform, [f"/data/games/{stem}.iso"], fmt,
                                 extra=("--delete-source",))
        # Control: a plain rename keeps the hashes, so RomM should reassociate.
        os.rename(self.library / "ps2" / "Rename PS2.iso",
                  self.library / "ps2" / "Rename PS2 (moved).iso")
        scan = self.scan({"type": "quick"}, label="quick scan after app conversions")
        out = []
        for stem, platform, fmt in REIDS + [("Rename PS2", "ps2", "iso")]:
            rid = self.ids[stem]
            old = self.snapshot(rid)
            rows_now = self.roms_named(stem)
            new_rows = [r for r in rows_now if int(r["id"]) != rid]
            out.append({
                "stem": stem, "format": fmt, "rom_id": rid,
                "kept_id": old["rom"]["fs_name"] != f"{stem}.iso"
                and old["rom"]["missing_from_fs"] == "0",
                "old_row_after": old["rom"], "old_user_data_after": old["user_data"],
                "new_rows": new_rows,
                "before": (before.get(stem) or rename_before)["rom"],
            })
        self.results["reidentify"] = {"app_cli": rows, "scan": scan["final"], "roms": out}

    def refusals(self):
        """E5: a rename RomM refuses, and one whose file is already gone."""
        folder = self.library / "ps2"
        rid = self.ids["Refuse PS2"]
        shutil.copyfile(self.work / "fixtures" / "ps2" / "Orphan PS2.iso",
                        folder / "Refuse PS2.chd")
        before = self.snapshot(rid)
        status, body = self.http("PUT", f"/api/roms/{rid}", who="swap",
                                 form={"fs_name": "Refuse PS2.chd"},
                                 label="rename onto an existing file")
        after = self.snapshot(rid)
        conflict = {"status": status, "body": body, "before": before["rom"],
                    "after": after["rom"],
                    "iso_still_there": (folder / "Refuse PS2.iso").exists()}
        orid = self.ids["Orphan PS2"]
        (folder / "Orphan PS2.iso").unlink()
        o_status, o_body = self.http("PUT", f"/api/roms/{orid}", who="swap",
                                     form={"fs_name": "Orphan PS2.chd"},
                                     label="rename a record whose file is gone")
        same_status, same_body = self.http(
            "PUT", f"/api/roms/{self.ids['Swap PS2']}", who="swap",
            form={"fs_name": "Swap PS2.chd"}, label="rename to the name it already has")
        bad_status, bad_body = self.http(
            "PUT", f"/api/roms/{self.ids['Stealth PS2']}", who="swap",
            form={"fs_name": "../escape.iso"}, label="rename with a path in it")
        bad_after = self.rom_row(self.ids["Stealth PS2"])
        self.results["refusals"] = {
            "conflict": conflict,
            "file_gone": {"status": o_status, "body": o_body,
                          "row_after": self.rom_row(orid)},
            "same_name": {"status": same_status,
                          "fs_name": same_body.get("fs_name")
                          if isinstance(same_body, dict) else same_body},
            "path_in_name": {"status": bad_status, "body": bad_body,
                             "fs_name_after": bad_after["fs_name"],
                             "escaped_file": (self.library / "escape.iso").exists()
                             or (self.work / "library" / "escape.iso").exists()},
        }

    def permissions(self):
        """E6: each swap call with a token missing one scope."""
        rid = self.ids["Swap GC"]
        name = self.rom_row(rid)["fs_name"]
        probes = [
            ("GET /api/roms/{id}", "GET", f"/api/roms/{rid}", None, None),
            ("PUT /api/roms/{id} (same name)", "PUT", f"/api/roms/{rid}", None,
             {"fs_name": name}),
            ("PUT /api/roms/2147483647 (probe)", "PUT", "/api/roms/2147483647", None,
             {"fs_name": "x.chd"}),
            ("POST /api/tasks/scan (hashes)", "POST", "/api/tasks/scan",
             {"type": "hashes", "roms_ids": [rid], "apis": []}, None),
            ("GET /api/tasks/status", "GET", "/api/tasks/status", None, None),
            ("GET /api/config", "GET", "/api/config", None, None),
            ("GET /api/heartbeat", "GET", "/api/heartbeat", None, None),
        ]
        matrix = {}
        for token in ("swap", "no_roms_write", "no_tasks_run", "no_roms_read", "read_only"):
            row = {}
            for label, method, path, body, form in probes:
                status, resp = self.http(method, path, who=token, json_body=body, form=form,
                                         label=f"perm {token} {label}")
                row[label] = status
                if status == 202 and isinstance(resp, dict):
                    self.wait_task(resp["task_id"], "full")
            matrix[token] = row
        # An editor can hold roms.write but not tasks.run.
        editor = ("spikeeditor", secrets.token_urlsafe(18))
        self.http("POST", "/api/users", basic=self.admin,
                  json_body={"username": editor[0], "email": "editor@example.invalid",
                             "password": editor[1], "role": "editor"},
                  label="create editor user")
        with_tasks = self.http("POST", "/api/client-tokens", basic=editor,
                               json_body={"name": "editor-swap", "scopes": SWAP_SCOPES},
                               label="editor mints a token with tasks.run")
        status, body = self.http("POST", "/api/client-tokens", basic=editor,
                                 json_body={"name": "editor-rename",
                                            "scopes": ["roms.read", "roms.write"]},
                                 label="editor mints a token with roms.write")
        editor_put = None
        if status == 201:
            self.tokens["editor"] = body["raw_token"]
            editor_put = self.http("PUT", f"/api/roms/{rid}", who="editor",
                                   form={"fs_name": name}, label="editor token renames")[0]
        self.results["permissions"] = {
            "matrix": matrix,
            "editor": {"mint_with_tasks_run": with_tasks, "mint_with_roms_write": status,
                       "put_with_editor_token": editor_put}}

    def converter_config(self):
        """E7: where RomM exposes its converter switch and per-platform targets."""
        out = {}
        _, hb = self.http("GET", "/api/heartbeat", label="heartbeat, converter off")
        _, cfg = self.http("GET", "/api/config", label="config, converter off")
        _, tasks = self.http("GET", "/api/tasks", who="full", label="tasks, converter off")
        out["off"] = {"heartbeat_CONVERTO": hb.get("CONVERTO"),
                      "config_CONVERTO": cfg.get("CONVERTO"),
                      "config_CONVERTO_LIBRARY_TARGETS": cfg.get("CONVERTO_LIBRARY_TARGETS"),
                      "convert_library_task": find_task(tasks, "convert_library")}
        status, body = self.http(
            "PUT", "/api/config/converto_settings", who="full",
            json_body={"download_conversion_enabled": False, "cache_max_size_gb": 20,
                       "platform_formats": {"ps2": "chd", "ngc": "rvz"}},
            label="set converter targets")
        _, cfg2 = self.http("GET", "/api/config", label="config after setting targets")
        out["set_targets"] = {"status": status, "body": body,
                              "config_CONVERTO": cfg2.get("CONVERTO"),
                              "config_yml": (self.work / "config" / "config.yml").read_text(
                                  encoding="utf-8")}
        status, body = self.http(
            "PUT", "/api/config/converto_settings", who="swap",
            json_body={"download_conversion_enabled": False, "cache_max_size_gb": 20,
                       "platform_formats": {}},
            label="set converter targets with the swap token")
        out["set_targets_with_swap_token"] = status
        env_file = self.work / ".env"
        env = env_file.read_text(encoding="utf-8").replace("ROM_CONVERTO_ENABLED=false",
                                                           "ROM_CONVERTO_ENABLED=true")
        env_file.write_text(env, encoding="utf-8")
        self.compose("up", "-d", "--force-recreate", "romm")
        self.wait_heartbeat()
        _, hb = self.http("GET", "/api/heartbeat", label="heartbeat, converter env on")
        _, tasks = self.http("GET", "/api/tasks", who="full", label="tasks, converter env on")
        binary = self.compose("exec", "-T", "romm", "sh", "-c",
                              "command -v rom-converto || echo absent", check=False).stdout
        out["env_on"] = {"heartbeat_CONVERTO": hb.get("CONVERTO"),
                         "convert_library_task": find_task(tasks, "convert_library"),
                         "rom_converto_binary": binary.strip()}
        self.results["converter"] = out

    def run(self):
        self.setup()
        _, hb = self.http("GET", "/api/heartbeat", label="version")
        app = docker("image", "inspect", self.app_image, "--format", "{{.Id}}").stdout.strip()
        self.results["versions"] = {"romm": hb["SYSTEM"]["VERSION"],
                                    "title_id_extraction": hb["FILESYSTEM"].get(
                                        "TITLE_ID_EXTRACTION_ENABLED"),
                                    "app_image": self.app_image, "app_image_id": app}
        self.initial_scan()
        self.seed_user_data()
        self.stage_swaps()
        for stem, platform, fmt, rescan in SWAPS:
            print(f"swap {stem} -> {fmt} ({rescan} scan) ...")
            self.swap(stem, platform, fmt, rescan)
        self.stability()
        self.stealth()
        self.race()
        self.reidentify()
        self.refusals()
        self.permissions()
        self.converter_config()
        (self.evidence / "results.json").write_text(json.dumps(self.results, indent=2),
                                                    encoding="utf-8")
        summarize(self.results)


def docker(*args, check=True, stdin=None):
    """Run the Docker CLI with an argument list, never a shell."""
    return subprocess.run(["/usr/bin/env", "docker", *args], check=check, capture_output=True,
                          text=True, input=stdin)


def local_request(port, method, path, data=None, headers=None, timeout=120):
    """One HTTP request to RomM on 127.0.0.1. Returns (status, body bytes, cookies)."""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        conn.request(method, path, body=data, headers=headers or {})
        resp = conn.getresponse()
        cookies = {}
        for header in resp.headers.get_all("Set-Cookie") or []:
            name, _, rest = header.partition("=")
            cookies[name.strip()] = rest.split(";", 1)[0]
        return resp.status, resp.read(), cookies
    finally:
        conn.close()


def find_task(grouped, name):
    for tasks in (grouped or {}).values():
        for task in tasks if isinstance(tasks, list) else []:
            if task.get("name") == name or task.get("task_name") == name:
                return task
    return None


def summarize(r):
    print("\n=== RomM", r["versions"]["romm"], "| app image", r["versions"]["app_image_id"][:19])
    s = r["staging"]
    seen = [(x["fs_name"], x["fs_path"]) for x in s["rows_for_staged_names"]]
    print(f"staged outputs seen by a quick scan: roms {s['roms_before']} -> "
          f"{s['roms_after']}, rows: {seen}")
    st = r["after_swaps_quick_scan"]
    ids = [(k, [x["id"] for x in v]) for k, v in st["rows"].items()]
    print(f"after all swaps, quick scan: rows {ids}, missing {st['missing']}")
    for sw in r["swaps"]:
        b, a = sw["before"]["rom"], sw["after"]["rom"]
        ud = {k: len(v) for k, v in sw["after"]["user_data"].items()}
        print(f"swap {sw['stem']} -> {sw['format']} via {sw['rescan']}: PUT {sw['put']['status']}"
              f", id {b['id']} -> {a['id']}, fs_name {a['fs_name']}, size {b['fs_size_bytes']} ->"
              f" {a['fs_size_bytes']} (file {sw['new_file_size']}), sha1 matches new file: "
              f"{a['sha1_hash'] == sw['new_file_sha1']}, title_id {b['title_id']} -> "
              f"{a['title_id']}, user data {ud}, rows for stem {len(sw['rows_for_stem'])}")
    st = r["stealth"]
    print(f"stealth swap (same size+mtime): quick scan sha1 updated: "
          f"{st['after_quick']['rom']['sha1_hash'] == st['new_sha1']}; hashes scan sha1 updated:"
          f" {st['after_hashes']['rom']['sha1_hash'] == st['new_sha1']}")
    for x in r["reidentify"]["roms"]:
        print(f"reidentify {x['stem']} ({x['format']}): kept id {x['kept_id']}; old row "
              f"{x['old_row_after']['fs_name']} missing={x['old_row_after']['missing_from_fs']}"
              f"; new rows {[(n['id'], n['fs_name'], n['title_id']) for n in x['new_rows']]}")
    ra = r["race"]
    print(f"race (scan between write and delete): rows between "
          f"{[(x['id'], x['fs_name'], x['missing_from_fs']) for x in ra['rows_between']]}, "
          f"after {[(x['id'], x['fs_name'], x['missing_from_fs']) for x in ra['rows_after']]}")
    rf = r["refusals"]
    print(f"rename onto existing file: {rf['conflict']['status']} {rf['conflict']['body']}")
    print(f"rename with file gone: {rf['file_gone']['status']} {rf['file_gone']['body']}")
    print(f"rename to same name: {rf['same_name']['status']}; with a path: "
          f"{rf['path_in_name']['status']} -> {rf['path_in_name']['fs_name_after']}")
    for token, row in r["permissions"]["matrix"].items():
        print(f"perm {token}: {row}")
    ed = r["permissions"]["editor"]
    print(f"editor: mint with tasks.run {ed['mint_with_tasks_run']}, mint with roms.write "
          f"{ed['mint_with_roms_write']}, PUT with that token {ed['put_with_editor_token']}")
    c = r["converter"]
    print(f"converter off: heartbeat {c['off']['heartbeat_CONVERTO']}, config "
          f"{c['off']['config_CONVERTO']}, task {c['off']['convert_library_task']}")
    print(f"set targets: {c['set_targets']['status']} -> "
          f"{c['set_targets']['config_CONVERTO']}; with swap token "
          f"{c['set_targets_with_swap_token']}")
    print(f"converter env on: {c['env_on']}")


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--work", type=Path, required=True,
                        help="scratch directory; wiped and recreated")
    parser.add_argument("--port", type=int, default=18980)
    parser.add_argument("--app-image", default="compressatorium:smoke-latest",
                        help="Compressatorium image whose CLI converts the fixtures")
    parser.add_argument("--keep", action="store_true", help="leave the stack running")
    args = parser.parse_args()
    spike = Spike(args.work.resolve(), args.port, args.app_image)
    try:
        spike.run()
    finally:
        if not args.keep:
            spike.teardown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
