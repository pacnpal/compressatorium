"""Every Library the app has: what a Rule scheduler sweeps.

One list for both schedulers, the server's (``rules.sweep.run_forever``, via
``romm.auto``) and ``compressatorium run-rules`` (#307), so a Library added
here is swept by both. RomM is the only Library so far.
"""

from __future__ import annotations

from services.rules.library import Library


def all_libraries() -> list[Library]:
    """Every Library, in sweep order."""
    from services.romm.library import romm_library

    return [romm_library]


async def load_settings() -> None:
    """Read each Library's saved settings, as the server does at startup.

    A process that sweeps without the server (``run-rules``) calls this first:
    RomM keeps its connection and Rule defaults in a cache the server primes
    when it starts.
    """
    from services.romm import settings as romm_settings

    await romm_settings.load()
