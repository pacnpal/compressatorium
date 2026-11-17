"""The RomM swap journal (#391): each swap's steps, written before they run.

A swap (``services.romm.swap_referrer``) renames a ROM's file in RomM, parks
the Source RomM moved, places the output, rehashes the ROM and deletes the
parked Source. Each of those steps is recorded here *before* it runs, so a
crash leaves the step it was on, and the next run can tell what to finish or
undo (#392).

States, in the order a swap moves through them:

* ``renaming``: ``PUT /api/roms/{id}`` with the output's name is about to be,
  or has been, sent. RomM may or may not have moved the Source.
* ``parking``: RomM confirmed the new name; the Source (now under the
  output's name) is being moved into a ``park-`` slot.
* ``parked``: it is there (``parked_path``). Recorded by the move itself, on
  its thread, so a cancel can't skip it.
* ``placing``: the output is being moved into place.
* ``placed``: it is there, the Source still parked.
* ``rehashing``: RomM is rehashing the ROM and the read-back is being checked.
* ``deleting``: the read-back passed; the parked Source is being deleted.

And the two undo steps a failure takes:

* ``reverting``: RomM is being asked to put the old name back (the Source
  moves back with it).
* ``restoring``: the parked Source is being moved back under its own name,
  beside the output, which keeps the ROM's id.

A swap ends ``done``, ``refused`` (RomM refused the rename, or wrote another
name, which was put back), ``failed`` (RomM couldn't rename, nothing moved),
``rolled_back`` (undone after RomM renamed) or ``kept`` (the ROM keeps its id
on the output, and the Source was put back beside it). A row still on a step
is unfinished: the step or its undo failed or the process died, and
``detail`` says why. Nothing sweeps a parked Source away, so it stays until
the next run settles the row.

Everything here is blocking: callers run it off the event loop.
"""

from __future__ import annotations

from services import db as _db

STEPS = (
    "renaming", "parking", "parked", "placing", "placed", "rehashing", "deleting",
    "reverting", "restoring",
)
ENDINGS = ("done", "refused", "failed", "rolled_back", "kept")


def _session():
    if _db.SessionLocal is None:
        raise RuntimeError("db.SessionLocal not initialized")
    return _db.SessionLocal()


def begin(
    *, rom_id: int, source_path: str, output_path: str, staged_path: str | None,
    old_name: str, new_name: str,
) -> int:
    """Record a swap about to send its rename: a new row in ``renaming``.
    Returns its id."""
    now = _db.utcnow_iso()
    with _session() as session:
        row = _db.RommSwap(
            rom_id=rom_id, source_path=source_path, output_path=output_path,
            staged_path=staged_path, old_name=old_name, new_name=new_name,
            state="renaming", created_at=now, updated_at=now,
        )
        session.add(row)
        session.commit()
        return int(row.id)


def step(
    row_id: int, state: str, *, detail: str | None = None,
    parked_path: str | None = None,
) -> None:
    """Move row *row_id* to *state* (a step about to run, or an ending).
    Raises when the row can't be written, so the step is never run
    unrecorded."""
    if state not in STEPS and state not in ENDINGS:
        raise ValueError(f"Not a swap state: {state!r}")
    values: dict = {"state": state, "updated_at": _db.utcnow_iso(), "detail": detail}
    if parked_path is not None:
        values["parked_path"] = parked_path
    with _session() as session:
        updated = session.query(_db.RommSwap).filter(_db.RommSwap.id == row_id).update(
            values, synchronize_session=False,
        )
        session.commit()
    if not updated:
        raise LookupError(f"No swap journal row {row_id}")


def current(row_id: int) -> str:
    """Row *row_id*'s state. Raises LookupError when there is no such row."""
    with _session() as session:
        row = session.get(_db.RommSwap, row_id)
        if row is None:
            raise LookupError(f"No swap journal row {row_id}")
        return row.state


def _as_dict(row: _db.RommSwap) -> dict:
    return {
        "id": row.id, "rom_id": row.rom_id, "source_path": row.source_path,
        "output_path": row.output_path, "staged_path": row.staged_path,
        "parked_path": row.parked_path, "old_name": row.old_name,
        "new_name": row.new_name, "state": row.state, "detail": row.detail,
        "created_at": row.created_at, "updated_at": row.updated_at,
    }


def rows() -> list[dict]:
    """Every row, oldest first."""
    with _session() as session:
        return [_as_dict(r) for r in session.query(_db.RommSwap).order_by(_db.RommSwap.id)]


def unfinished() -> list[dict]:
    """The rows still on a step, oldest first: what the next run must settle."""
    with _session() as session:
        found = (
            session.query(_db.RommSwap)
            .filter(_db.RommSwap.state.in_(STEPS))
            .order_by(_db.RommSwap.id)
        )
        return [_as_dict(r) for r in found]
