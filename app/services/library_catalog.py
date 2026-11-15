"""Every Library the app has: what a Rule scheduler sweeps.

One list for both schedulers, the server's (``rules.sweep.run_forever``, via
``romm.auto``) and ``compressatorium run-rules`` (#307), so a Library added
here is swept by both. RomM is the only Library so far. Callers ask for the
list once the database is open, so a Library stored there can be listed.
"""

from __future__ import annotations

from services.rules.library import Library


def all_libraries() -> list[Library]:
    """Every Library, in sweep order."""
    from services.romm.library import romm_library

    return [romm_library]
