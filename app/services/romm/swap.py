"""The swap switch's token check (#390).

"Keep the ROM id when converting" (#381) renames RomM's record and rescans it
instead of letting RomM add a new one, so the token must hold ``roms.read`` (the
read-back), ``roms.write`` (the rename) and ``tasks.run`` (the rescan). RomM
cannot list a token's scopes; ``RommClient.token_permissions`` asks it with
three probes, and this module turns the answer into the wording and the status
block. The swap itself is #391 and reads the setting this one stores.
"""

from __future__ import annotations

from services.romm.client import RommClient

REQUIRED_SCOPES = ("roms.read", "roms.write", "tasks.run")

# RomM hands out roms.write and tasks.run only to an admin user (or a group
# granting ROM write and task write): a default user asking for them gets a 403.
ADMIN_HINT = (
    "The token has to be created by an admin user (or a user in a group that "
    "can write ROMs and run tasks) in RomM, under Administration, Client API "
    "Tokens."
)

NO_TOKEN = "Can't turn this on. No API token is set. " + ADMIN_HINT

# The longest the three probes may take together, with a margin over their own
# timeouts, so a RomM that stops answering cannot hold a request.
CHECK_SECONDS = 40


def missing_scopes(permissions: dict[str, bool]) -> list[str]:
    """The required scopes *permissions* says the token lacks, in a fixed order."""
    return [scope for scope in REQUIRED_SCOPES if not permissions.get(scope)]


def problem(missing: list[str]) -> str:
    """What is wrong with a token that lacks *missing*, naming each one."""
    return f"The token is missing {', '.join(missing)}. " + ADMIN_HINT


def refusal(missing: list[str]) -> str:
    """The message for refusing to turn the switch on."""
    return "Can't turn this on. " + problem(missing)


def check(client: RommClient) -> list[str]:
    """Probe the token with *client* and return its missing scopes.

    Blocking: callers run it through the threadpool, under ``CHECK_SECONDS``.
    A RomM that cannot answer raises ``RommError`` rather than returning a
    guess.
    """
    return missing_scopes(client.token_permissions())
