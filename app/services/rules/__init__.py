"""The Rule engine, shared by every Library.

* :mod:`~services.rules.library` -- the Library interface: Platforms, Images,
  capabilities and the hooks for steps only one Library performs;
* :mod:`~services.rules.rules` -- the Rule schema, defaults and validation;
* :mod:`~services.rules.schedule` -- weekday mask, time window, interval;
* :mod:`~services.rules.store` -- the stored Rules and state, keyed
  ``"<library id>:<platform id>"``, and the one-time move of RomM's old keys;
* :mod:`~services.rules.sweep` -- the sweep, the converted history and the
  scheduler.

RomM is the first Library (:mod:`services.romm.library`); folders registered
as Libraries are the second (:mod:`services.libraries.folder`).
"""

from .library import (
    CONVERTED_HISTORY,
    FAILED_HOLD,
    RECOGNIZED_FILTER,
    Image,
    Library,
    LibraryUnreadable,
    Platform,
    QueuedImage,
    UnsupportedPolicy,
    duplicate_actions,
    refuse_unsupported_policies,
)

__all__ = [
    "CONVERTED_HISTORY",
    "FAILED_HOLD",
    "RECOGNIZED_FILTER",
    "Image",
    "Library",
    "LibraryUnreadable",
    "Platform",
    "QueuedImage",
    "UnsupportedPolicy",
    "duplicate_actions",
    "refuse_unsupported_policies",
]
