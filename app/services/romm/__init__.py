"""RomM integration services.

Four concerns, one package, in the order a conversion meets them:

* :mod:`~services.romm.settings` — what instance, which library, and every
  in-app preference the other three read;
* :mod:`~services.romm.client` — the HTTP surface: the bounded, redirect-safe
  opener, the path mapper that turns a RomM record into a local path, and the
  multipart writer that pushes metadata back;
* :mod:`~services.romm.library` and :mod:`~services.romm.auto` — RomM as a
  Library for the shared Rule engine in :mod:`services.rules`, and the
  RomM-bound Rule functions the routes call;
* :mod:`~services.romm.repin` — the metadata queue: what was recorded before a
  conversion, and how it is re-applied once RomM rescans.

The client's names are re-exported here because they are the package's public
face — ``from services.romm import romm_client`` reads better at a call site
than the module path, and it is what every caller already used when this was a
single module.
"""
from .client import (
    DAT_SAFE_OUTPUT_EXTS,
    METADATA_ID_FIELDS,
    UNHASHED_PLATFORM_SLUGS,
    RommClient,
    RommError,
    RommNotConfigured,
    heartbeat_version,
    romm_client,
)

__all__ = [
    "DAT_SAFE_OUTPUT_EXTS",
    "METADATA_ID_FIELDS",
    "UNHASHED_PLATFORM_SLUGS",
    "RommClient",
    "RommError",
    "RommNotConfigured",
    "heartbeat_version",
    "romm_client",
]
