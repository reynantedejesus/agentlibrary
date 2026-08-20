"""Server-side WGT code allocation.

Format: ``WGT`` + department digit + zero-padded sequence, e.g. Finance's first
two assets are ``WGT201`` / ``WGT202``. The digit is the department's 1-based
position in ``departmentDigitOrder`` (the numbering order, deliberately
independent of the display order). Sequences past 99 simply grow a third digit
(``WGT2100``) — that is base-10, not a collision, because the digit prefix is
consumed left-to-right and the sequence is always the remainder.

Race-condition handling
-----------------------
The prototype computed ``max(existing)+1`` in the browser, so two people
submitting at the same moment produced the same code. Here:

1. ``SELECT ... FOR UPDATE`` on the ``wgt_counters`` row for that department
   digit serialises concurrent allocators inside one transaction. On MySQL
   InnoDB the second transaction blocks until the first commits.
2. ``assets.wgt_code`` carries a ``UNIQUE`` constraint, so even if the counter
   were bypassed (manual insert, imported legacy data) the database refuses a
   duplicate.
3. ``allocate_with_retry()`` retries a bounded number of times on
   ``IntegrityError``, re-syncing the counter from the real table first.

SQLite (used by the test-suite) ignores ``FOR UPDATE`` but serialises writes at
the file level, and the UNIQUE constraint plus retry still hold.

Connects to: ``app/api/assets.py`` (submission), ``app/cli.py`` (seeding the
counter rows, legacy import).
"""
from __future__ import annotations

import logging
import re
from typing import Optional

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError

from app import reference
from app.errors import Conflict
from app.extensions import db
from app.models import Asset, WgtCounter

log = logging.getLogger(__name__)

WGT_PATTERN = re.compile(r"^WGT(\d+)$")
SEQUENCE_PAD = 2
MAX_ALLOCATION_ATTEMPTS = 6


def prefix_for_department(department: str) -> str:
    return "WGT" + reference.department_digit(department)


def format_code(prefix: str, sequence: int) -> str:
    return "{0}{1}".format(prefix, str(sequence).zfill(SEQUENCE_PAD))


def _parse_sequence(code: str, digit: str) -> Optional[int]:
    """Return the sequence number ``code`` encodes for ``digit``, else None.

    Codes for different departments can share a textual prefix — Sales is
    digit "1" and Company Wide is digit "10", so ``WGT101`` and ``WGT1001``
    both begin ``WGT10``. They stay unambiguous because the sequence is
    zero-padded to exactly SEQUENCE_PAD and only grows *past* that width
    without a leading zero:

        WGT101   digit 1,  seq 01   (tail "01"  — exactly 2 chars)
        WGT1001  digit 10, seq 01   (tail "001" for digit 1 has a leading
                                     zero, so digit 1 rejects it)
        WGT1100  digit 1,  seq 100  (tail "100" — longer than 2, no leading
                                     zero; digit 10 sees tail "0" and rejects)
    """
    match = WGT_PATTERN.match(code or "")
    if not match:
        return None
    digits = match.group(1)
    if not digits.startswith(digit):
        return None
    tail = digits[len(digit):]
    if not tail.isdigit():
        return None
    if len(tail) < SEQUENCE_PAD:
        return None
    if len(tail) > SEQUENCE_PAD and tail[0] == "0":
        return None
    return int(tail)


def _highest_existing_sequence(prefix: str) -> int:
    """Largest sequence already present in ``assets`` for this prefix.

    Used to seed or repair a counter row: the counter is authoritative once it
    exists, but it must never hand out a code an imported asset already owns.
    """
    digit = prefix[3:]
    highest = 0
    rows = (db.session.query(Asset.wgt_code)
            .filter(Asset.wgt_code.like(prefix + "%"))
            .all())
    for (code,) in rows:
        sequence = _parse_sequence(code, digit)
        if sequence is not None and sequence > highest:
            highest = sequence
    return highest


def _locked_counter(prefix: str) -> WgtCounter:
    """Fetch (creating if needed) the counter row with a write lock held."""
    digit = prefix[3:]
    counter = (db.session.query(WgtCounter)
               .filter(WgtCounter.department_digit == digit)
               .with_for_update()
               .one_or_none())
    if counter is None:
        counter = WgtCounter(department_digit=digit,
                             last_sequence=_highest_existing_sequence(prefix))
        db.session.add(counter)
        db.session.flush()
        # Re-select with the lock now that the row exists.
        counter = (db.session.query(WgtCounter)
                   .filter(WgtCounter.department_digit == digit)
                   .with_for_update()
                   .one())
    return counter


def allocate(department: str) -> str:
    """Reserve and return the next WGT code for ``department``.

    Must be called inside the same transaction that inserts the asset — the
    counter increment and the asset row commit together, so an abandoned
    submission never burns a code.
    """
    prefix = prefix_for_department(department)
    counter = _locked_counter(prefix)

    # Defensive re-sync: if the assets table has drifted ahead of the counter
    # (legacy import, manual insert), catch up rather than issue a duplicate.
    existing_high = _highest_existing_sequence(prefix)
    if existing_high > counter.last_sequence:
        counter.last_sequence = existing_high

    counter.last_sequence = int(counter.last_sequence or 0) + 1
    db.session.flush()
    return format_code(prefix, counter.last_sequence)


def allocate_with_retry(department: str, insert_callback, attempts: int = MAX_ALLOCATION_ATTEMPTS):
    """Allocate a code, hand it to ``insert_callback(code)``, and commit.

    ``insert_callback`` must add its object(s) to ``db.session`` and return the
    primary object; this function performs the ``flush``/``commit``. On a
    ``UNIQUE`` violation of ``assets.wgt_code`` the whole transaction is rolled
    back and retried with a freshly-read counter.
    """
    last_error: Optional[Exception] = None
    for attempt in range(1, attempts + 1):
        try:
            code = allocate(department)
            result = insert_callback(code)
            db.session.commit()
            return result
        except IntegrityError as exc:
            db.session.rollback()
            last_error = exc
            message = str(getattr(exc, "orig", exc)).lower()
            if "wgt_code" not in message and "unique" not in message:
                raise
            log.warning("WGT allocation collision on attempt %s for %s: %s",
                        attempt, department, message[:200])
            _resync_counter(department)
    log.error("Exhausted WGT allocation attempts for %s", department)
    raise Conflict(
        "Could not allocate a WGT reference code — please try again.",
        code="WGT_ALLOCATION_FAILED",
    ) from last_error


def _resync_counter(department: str) -> None:
    """Push the counter past whatever the assets table actually holds."""
    prefix = prefix_for_department(department)
    digit = prefix[3:]
    try:
        counter = (db.session.query(WgtCounter)
                   .filter(WgtCounter.department_digit == digit)
                   .with_for_update()
                   .one_or_none())
        highest = _highest_existing_sequence(prefix)
        if counter is None:
            db.session.add(WgtCounter(department_digit=digit, last_sequence=highest))
        elif highest > counter.last_sequence:
            counter.last_sequence = highest
        db.session.commit()
    except Exception:       # pragma: no cover - defensive
        db.session.rollback()
        log.exception("Failed to resync WGT counter for %s", department)


def preview_next_code(department: str) -> str:
    """Non-authoritative preview for the wizard's "your code will be…" hint.

    Explicitly NOT a reservation: two people previewing at the same time see
    the same value, and the real code is only fixed at insert time.
    """
    prefix = prefix_for_department(department)
    digit = prefix[3:]
    counter = (db.session.query(WgtCounter)
               .filter(WgtCounter.department_digit == digit)
               .one_or_none())
    current = int(counter.last_sequence) if counter else 0
    current = max(current, _highest_existing_sequence(prefix))
    return format_code(prefix, current + 1)


def ensure_counter_rows() -> int:
    """Create one counter row per department digit. Idempotent; used by seeding."""
    created = 0
    order = reference.get_reference("departmentDigitOrder") or reference.DEPARTMENT_DIGIT_ORDER
    for index, _department in enumerate(order):
        digit = str(index + 1)
        if db.session.get(WgtCounter, digit) is None:
            prefix = "WGT" + digit
            db.session.add(WgtCounter(department_digit=digit,
                                      last_sequence=_highest_existing_sequence(prefix)))
            created += 1
    db.session.commit()
    return created


def counter_state() -> dict:
    rows = db.session.query(WgtCounter).all()
    return {r.department_digit: r.last_sequence for r in rows}


def total_assets() -> int:
    return int(db.session.query(func.count(Asset.id)).scalar() or 0)
