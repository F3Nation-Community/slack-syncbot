"""The two Slack ts converters.

:func:`slack_message_ts` is the string form (Slack API kwargs and log fields).
:func:`post_meta_ts` is the Decimal form for every DECIMAL(16, 6) ts column
(``post_meta.ts`` and the ``pending_events`` ts columns). Do not ``float()``
those columns, and do not add a third converter.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation


def slack_message_ts(ts: object) -> str:
    """Six-decimal Slack message ts as a string (API kwargs and comparisons of strings).

    Persist or compare a ts column with :func:`post_meta_ts`. ``str(Decimal)``
    drops trailing zeros, so string keys must not use a raw Decimal or float.
    """
    raw = str(ts).strip()
    if not raw:
        return raw
    if "." in raw:
        whole, frac = raw.split(".", 1)
        return f"{whole}.{(frac + '000000')[:6]}"
    return f"{raw}.000000"


def post_meta_ts(ts: object) -> Decimal:
    """The only value to persist or compare on a DECIMAL(16, 6) ts column.

    A current Slack ts does not fit in a Python float, so ``float(ts)`` misses
    existing rows. Do not add another converter.
    """
    padded = slack_message_ts(ts)
    if not padded:
        raise ValueError("empty Slack message ts")
    try:
        return Decimal(padded)
    except (InvalidOperation, ArithmeticError) as exc:
        raise ValueError(f"invalid Slack message ts {ts!r}") from exc
