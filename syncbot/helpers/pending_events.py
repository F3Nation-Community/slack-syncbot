"""Pending events: rows SyncBot keeps while one event waits on another. Slack addresses only.

A *pending event* is any row of ``pending_events``. Its ``kind`` says which:

* :data:`MESSAGE_ECHO` and :data:`FILE_ECHO`: SyncBot wrote as a mapped person
  (``xoxp``), so Slack's event names that person, not the bot. Checked, never
  taken, since one write can bring more than one event (a ``file_share``, then
  a ``message_changed``).
* :data:`REACTION_ADDED_ECHO` and :data:`REACTION_REMOVED_ECHO`: a reaction
  SyncBot placed as a mapped person. The kind is Slack's event type, and the
  matching event takes the row once.
* :data:`REACTION_ORDER`: a reaction ``event_ts`` applied on a target, so an
  older event that arrives later is skipped (:func:`reaction_event_ts_is_stale`).
* :data:`PENDING_SHARE`: a file apply whose share ts Slack had not returned
  yet. The inbound ``file_share`` finds it (:func:`find_pending_file_share`),
  writes the copy's PostMeta, and takes it.
* :data:`REPLAY`: a reply, edit, delete, or reaction that arrived before the
  message it acts on had PostMeta. That message's create takes it
  (:func:`take_pending_events_to_replay`) and replays it. A new reply waits on
  its parent (``source_thread_ts``); anything else waits on its own message.

The *target* columns are where SyncBot wrote a copy, or will write one. The
*source* columns are where a person acted. A row holds only addresses: ids,
timestamps, and a reaction's name, which is its id (Slack has no reaction id).
Anything Slack can give back from an address (text, blocks, files,
attachments, names, avatars) is read again, never stored.
``tests/test_pending_events.py`` pins the columns.

:func:`remember_pending_event`, :func:`check_pending_event`, and
:func:`take_pending_event` serve every kind. :data:`_COLUMNS` lists each kind's
columns, and every call is checked against it.

Only rows from the last :data:`_TTL` count. Handlers delete only the rows they
take, each with one atomic ``DELETE``. :func:`purge_expired_pending_events`
removes the rest on the scheduled pulse (``refresh_instance``).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import NamedTuple

from sqlalchemy import and_, or_

from db import close_session, get_session, schemas
from helpers.slack_ts import post_meta_ts
from logger import log_debug, log_warning

_TTL = timedelta(minutes=10)
_TS_COLUMNS = frozenset({"target_ts", "source_ts", "source_thread_ts", "event_ts"})

MESSAGE_ECHO = "message"
FILE_ECHO = "file"
REACTION_ADDED_ECHO = "reaction_added"
REACTION_REMOVED_ECHO = "reaction_removed"
REACTION_ORDER = "reaction_event"
PENDING_SHARE = "pending_share"
REPLAY = "replay"

_Row = schemas.PendingEvent


class _Columns(NamedTuple):
    """One kind's columns. A check or take matches on *key*; a write needs *key* and *required*."""

    key: tuple[str, ...]
    required: tuple[str, ...] = ()
    optional: tuple[str, ...] = ()


_ECHO_KEY = ("target_team_id", "target_user_id", "target_channel_id", "target_ts")
_COLUMNS: dict[str, _Columns] = {
    MESSAGE_ECHO: _Columns(key=_ECHO_KEY),
    FILE_ECHO: _Columns(key=("target_team_id", "target_user_id", "file_id")),
    REACTION_ADDED_ECHO: _Columns(key=(*_ECHO_KEY, "reaction")),
    REACTION_REMOVED_ECHO: _Columns(key=(*_ECHO_KEY, "reaction")),
    REACTION_ORDER: _Columns(
        key=("target_team_id", "source_user_id", "target_channel_id", "target_ts", "reaction"),
        required=("event_ts",),
    ),
    PENDING_SHARE: _Columns(
        key=("target_team_id", "target_channel_id", "file_id"),
        required=("post_id", "target_sync_channel_id"),
        optional=("source_team_id", "source_user_id", "target_user_id"),
    ),
    REPLAY: _Columns(
        key=("source_team_id", "source_channel_id", "source_ts", "action"),
        optional=("source_thread_ts", "source_user_id", "reaction", "event_ts"),
    ),
}


@dataclass(frozen=True)
class PendingFileShare:
    """The copy's PostMeta, waiting for Slack's share ts. Field names are PostMeta's columns."""

    post_id: str
    sync_channel_id: int
    source_user_id: str | None = None
    source_workspace_id: int | None = None
    posted_as_user_id: str | None = None


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _live():
    """Only rows from the last :data:`_TTL` count, whether or not the sweep has run."""
    return _Row.created_at >= _utcnow() - _TTL


def _empty(value) -> bool:
    return value is None or value == ""


def _typed(columns: dict) -> dict:
    """Column values, with every ts through :func:`post_meta_ts`."""
    return {
        name: post_meta_ts(value) if name in _TS_COLUMNS and value is not None else value
        for name, value in columns.items()
    }


def _spec(kind: str) -> _Columns:
    spec = _COLUMNS.get(kind)
    if spec is None:
        raise ValueError(f"unknown pending event kind {kind!r}")
    return spec


def _row_columns(kind: str, columns: dict) -> dict | None:
    """The columns to write, or ``None`` when a needed value is empty (nothing to remember).

    Raises when a column name is missing or is not one this kind uses.
    """
    spec = _spec(kind)
    needed = (*spec.key, *spec.required)
    unknown = set(columns) - {*needed, *spec.optional}
    missing = set(needed) - set(columns)
    if unknown or missing:
        raise ValueError(f"{kind} columns: unknown {sorted(unknown)}, missing {sorted(missing)}")
    if any(_empty(columns[name]) for name in needed):
        return None
    return _typed({name: value for name, value in columns.items() if not _empty(value)})


def _address(kind: str, columns: dict) -> dict | None:
    """The columns a check or take matches on, or ``None`` when a value is empty (no match).

    Either the kind's whole key or ``id`` alone. Anything else raises, since a
    partial key would match rows it should not (every echo of that person).
    """
    spec = _spec(kind)
    if set(columns) != set(spec.key) and set(columns) != {"id"}:
        raise ValueError(f"{kind} address must be {sorted(spec.key)} or id, not {sorted(columns)}")
    if any(_empty(value) for value in columns.values()):
        return None
    return _typed(columns)


def _matching(kind: str, address: dict) -> list:
    return [_Row.kind == kind, _live(), *(getattr(_Row, name) == value for name, value in address.items())]


def remember_pending_event(kind: str, **columns) -> int | None:
    """Write one pending event of *kind*. Its id, or ``None`` when nothing was written.

    Pass every column the kind needs (:data:`_COLUMNS`). A needed value that is
    empty writes nothing, without a warning.
    """
    session = get_session()
    try:
        values = _row_columns(kind, columns)
        if values is None:
            return None
        row = _Row(kind=kind, created_at=_utcnow(), **values)
        session.add(row)
        session.flush()
        pending_id = row.id
        session.commit()
        return pending_id
    except Exception as exc:
        session.rollback()
        log_warning("pending_event_failed", op="remember", kind=kind, error=str(exc))
        return None
    finally:
        close_session(session)


def check_pending_event(kind: str, **address) -> bool:
    """True when a live pending event of *kind* matches *address*. Does not take it."""
    session = get_session()
    try:
        values = _address(kind, address)
        if values is None:
            return False
        return session.query(_Row.id).filter(*_matching(kind, values)).first() is not None
    except Exception as exc:
        log_warning("pending_event_failed", op="check", kind=kind, error=str(exc))
        return False
    finally:
        close_session(session)


def take_pending_event(kind: str, **address) -> bool:
    """Delete the live pending events of *kind* that match *address*. True when there were any.

    One ``DELETE … WHERE`` and its rowcount, so two callers racing for the same
    row cannot both take it. Identical rows are taken together, once. ``id=``
    takes exactly one row.
    """
    session = get_session()
    try:
        values = _address(kind, address)
        if values is None:
            return False
        taken = session.query(_Row).filter(*_matching(kind, values)).delete(synchronize_session=False)
        session.commit()
        return bool(taken)
    except Exception as exc:
        session.rollback()
        log_warning("pending_event_failed", op="take", kind=kind, error=str(exc))
        return False
    finally:
        close_session(session)


def reaction_event_ts_is_stale(
    *,
    team_id: str,
    source_user_id: str | None,
    channel_id: str,
    ts: str,
    reaction: str,
    event_ts: str | None,
) -> bool:
    """True when this reaction already has an applied ``event_ts`` at or after *event_ts*."""
    session = get_session()
    try:
        address = _address(
            REACTION_ORDER,
            {
                "target_team_id": team_id,
                "source_user_id": source_user_id,
                "target_channel_id": channel_id,
                "target_ts": ts,
                "reaction": reaction,
            },
        )
        if address is None or _empty(event_ts):
            return False
        newer = _Row.event_ts >= post_meta_ts(event_ts)
        return session.query(_Row.id).filter(*_matching(REACTION_ORDER, address), newer).first() is not None
    except Exception as exc:
        log_warning("pending_event_failed", op="check", kind=REACTION_ORDER, error=str(exc))
        return False
    finally:
        close_session(session)


def find_pending_file_share(*, team_id: str, channel_id: str, file_id: str) -> PendingFileShare | None:
    """The newest live file apply for this Slack file share, as the copy's PostMeta. Does not take it."""
    session = get_session()
    try:
        address = _address(
            PENDING_SHARE, {"target_team_id": team_id, "target_channel_id": channel_id, "file_id": file_id}
        )
        if address is None:
            return None
        row = (
            session.query(_Row)
            .filter(*_matching(PENDING_SHARE, address))
            .order_by(_Row.created_at.desc(), _Row.id.desc())
            .first()
        )
        if row is None:
            return None
        source_workspace_id = None
        if row.source_team_id:
            source_workspace_id = (
                session.query(schemas.Workspace.id).filter(schemas.Workspace.team_id == row.source_team_id).scalar()
            )
        return PendingFileShare(
            post_id=row.post_id,
            sync_channel_id=row.target_sync_channel_id,
            source_user_id=row.source_user_id,
            source_workspace_id=source_workspace_id,
            posted_as_user_id=row.target_user_id,
        )
    except Exception as exc:
        log_warning("pending_event_failed", op="check", kind=PENDING_SHARE, error=str(exc))
        return None
    finally:
        close_session(session)


def take_pending_events_to_replay(
    *, team_id: str | None, channel_id: str | None, message_ts: str | None
) -> list[schemas.PendingEvent]:
    """Every live :data:`REPLAY` row waiting on *message_ts*, oldest first, taken.

    Each row is taken with its own ``DELETE`` by id, so it goes to one caller
    only. The rows come back detached, readable after the session closes.
    """
    if not team_id or not channel_id or not message_ts:
        return []
    session = get_session()
    try:
        ts = post_meta_ts(message_ts)
        rows = (
            session.query(_Row)
            .filter(
                _Row.kind == REPLAY,
                _live(),
                _Row.source_team_id == team_id,
                _Row.source_channel_id == channel_id,
                or_(_Row.source_thread_ts == ts, and_(_Row.source_thread_ts.is_(None), _Row.source_ts == ts)),
            )
            .order_by(_Row.created_at, _Row.id)
            .all()
        )
        taken = [row for row in rows if session.query(_Row).filter(_Row.id == row.id).delete(synchronize_session=False)]
        session.expunge_all()
        session.commit()
        return taken
    except Exception as exc:
        session.rollback()
        log_warning("pending_event_failed", op="take", kind=REPLAY, error=str(exc))
        return []
    finally:
        close_session(session)


def purge_expired_pending_events() -> int:
    """Delete every pending event older than :data:`_TTL`. Runs on the scheduled pulse, never in a handler.

    Never raises. Safe to run twice at once: each run deletes only expired rows.
    """
    session = get_session()
    try:
        removed = session.query(_Row).filter(_Row.created_at < _utcnow() - _TTL).delete(synchronize_session=False)
        session.commit()
        if removed:
            log_debug("pending_events_purged", count=removed)
        return removed
    except Exception as exc:
        session.rollback()
        log_warning("pending_event_failed", op="purge", error=str(exc))
        return 0
    finally:
        close_session(session)
