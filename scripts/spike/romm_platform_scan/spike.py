#!/usr/bin/env python3
"""RomM Platform scan check (#433): which call asks a real RomM to scan named
Platforms, and which token scope it needs.

The Inbox (#431) places new Images in a RomM Library and then asks RomM to
scan only the Platforms it added to. This runs that call against the same
pinned RomM the swap spike uses (``../romm_swap``), on a scratch library, and
records what happened. It reuses the swap spike's stack, fixtures, token setup
and work-folder guard, so the same rules apply (see ``../romm_swap/README.md``).

    python3 scripts/spike/romm_platform_scan/spike.py --work /some/scratch/dir

Writes WORK/evidence/calls.jsonl and WORK/evidence/results.json, prints a
summary, and tears the stack down unless --keep is given. Not a test; nothing
runs it automatically.
"""
import argparse
import importlib.util
import json
import shutil
import sys
from pathlib import Path


def _swap_spike():
    """The swap spike's module (``../romm_swap/spike.py``), loaded by path."""
    path = Path(__file__).resolve().parent.parent / "romm_swap" / "spike.py"
    spec = importlib.util.spec_from_file_location("romm_swap_spike", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_SWAP = _swap_spike()
SwapSpike = _SWAP.Spike
work_dir_state = _SWAP.work_dir_state

# A made-up cartridge: zeros with a name, no game in it.
SYNTHETIC_GB = b"\x00" * 0x8000


class PlatformScanSpike(SwapSpike):
    def tokens_for_scan(self):
        """One token with only ``tasks.run``, besides the swap spike's."""
        _, me = self.http("GET", "/api/users/me", basic=self.admin, label="admin scopes")
        for name, scopes in {"scan_only": ["tasks.run"]}.items():
            status, body = self.http(
                "POST", "/api/client-tokens", basic=self.admin,
                json_body={"name": f"spike-{name}", "scopes": scopes},
                label=f"create token {name}")
            if status != 201:
                raise RuntimeError(f"could not create token {name}: {status} {body}")
            self.tokens[name] = body["raw_token"]
        return me["oauth_scopes"]

    def names(self):
        return sorted(row["fs_name"] for row in self.sql("SELECT fs_name FROM roms"))

    def platform_slugs(self):
        return sorted(row["fs_slug"] for row in self.sql("SELECT fs_slug FROM platforms"))

    def run(self):
        self.setup()
        self.initial_scan()
        self.tokens_for_scan()
        before = {"roms": self.names(), "platforms": self.platform_slugs()}
        # New arrivals: one in a Platform RomM knows (ps2), one in a folder it
        # has no row for yet (gb), and one in a Platform not named in the scan
        # (psp), which must stay unseen.
        fixtures = self.work / "fixtures"
        shutil.copyfile(fixtures / "ps2" / "Swap PS2.iso", self.library / "ps2" / "New PS2.iso")
        shutil.copyfile(fixtures / "psp" / "Swap PSP.iso", self.library / "psp" / "New PSP.iso")
        (self.library / "gb").mkdir()
        (self.library / "gb" / "New GB.gb").write_bytes(SYNTHETIC_GB)

        payload = {"type": "quick", "platform_fs_slugs": ["ps2", "gb"]}
        refused = {}
        for who in ("no_tasks_run", "read_only"):
            status, body = self.http("POST", "/api/tasks/scan", who=who, json_body=payload,
                                     label=f"platform scan as {who}")
            refused[who] = {"status": status, "body": body}
        after_refused = self.names()

        scanned = self.scan(payload, who="scan_only", label="platform scan, tasks.run only")
        after = {"roms": self.names(), "platforms": self.platform_slugs()}

        unknown = self.scan({"type": "quick", "platform_fs_slugs": ["no-such-folder"]},
                            who="scan_only", label="platform scan of a folder that isn't there")
        status, body = self.http("POST", "/api/tasks/scan", who="scan_only",
                                 json_body={"type": "quick", "platform_slugs": ["ps2"]},
                                 label="misspelt key")
        self.results["platform_scan"] = {
            "before": before,
            "refused": refused,
            "roms_after_refusals": after_refused,
            "scan": scanned,
            "after": after,
            "unknown_folder": unknown,
            "misspelt_key": {"status": status, "body": body},
        }
        (self.evidence / "results.json").write_text(json.dumps(self.results, indent=2),
                                                    encoding="utf-8")
        summary = self.results["platform_scan"]
        print(json.dumps({
            "refused": {k: v["status"] for k, v in summary["refused"].items()},
            "new_roms": sorted(set(after["roms"]) - set(before["roms"])),
            "new_platforms": sorted(set(after["platforms"]) - set(before["platforms"])),
            "scan_status": scanned["final"].get("status"),
            "scan_stats": scanned["final"].get("meta"),
            "unknown_folder_stats": unknown["final"].get("meta"),
            "misspelt_key_status": status,
        }, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--work", type=Path, required=True,
                        help="scratch folder: new, empty, or marked by an earlier run")
    parser.add_argument("--port", type=int, default=18980)
    parser.add_argument("--keep", action="store_true", help="leave the stack running")
    args = parser.parse_args()
    work_dir_state(args.work)
    spike = PlatformScanSpike(args.work.absolute(), args.port, app_image=None)
    try:
        spike.run()
    finally:
        if not args.keep:
            spike.teardown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
