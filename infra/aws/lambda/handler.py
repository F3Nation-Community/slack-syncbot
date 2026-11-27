"""Lambda entry: SQLite + Litestream when LITESTREAM_S3_BUCKET is set, else app.handler.

Litestream lives here (infra), not in syncbot/. Existing MySQL/TiDB skips restore
and still relies on the post-deploy {"action":"migrate"} invoke.

The SQLite bootstrap and ``import app`` run at module load, in Lambda's init
phase, so a new execution environment does that work once, before its first
request.
"""

from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path

_logger = logging.getLogger(__name__)

_DB_PATH = Path("/tmp/syncbot.db")
_LITESTREAM_BIN = os.environ.get("LITESTREAM_BIN", "/var/task/litestream")
_LITESTREAM_CONFIG = os.environ.get("LITESTREAM_CONFIG", "/var/task/litestream.yml")


def _bootstrap_sqlite() -> None:
    """Restore (when /tmp has no database yet), migrate, then replicate."""
    if not _DB_PATH.exists():
        _logger.info("litestream: restoring from S3 (if replica exists)")
        subprocess.run(
            [
                _LITESTREAM_BIN,
                "restore",
                "-if-replica-exists",
                "-config",
                _LITESTREAM_CONFIG,
                str(_DB_PATH),
            ],
            check=True,
        )
    from db import initialize_database

    initialize_database()
    _logger.info("litestream: starting replicate")
    subprocess.Popen(
        [_LITESTREAM_BIN, "replicate", "-config", _LITESTREAM_CONFIG],
        start_new_session=True,
    )


if os.environ.get("LITESTREAM_S3_BUCKET"):
    _bootstrap_sqlite()

from app import handler  # noqa: E402

__all__ = ["handler"]
