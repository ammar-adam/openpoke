"""Shared, pre-request evaluation reservations, not a provider billing meter."""
from __future__ import annotations

import math
import sqlite3
import time
import uuid
from contextlib import contextmanager
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from pathlib import Path


class BudgetExhausted(RuntimeError):
    """No further model request was authorized by the evaluation budget."""


def positive_number(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be finite and positive")
    return value


def positive_integer(value, name):
    if isinstance(value, bool) or not isinstance(value, int) or not 0 < value <= 2**31 - 1:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _nanos(value, *, ceiling):
    amount = int((Decimal(str(value)) * 1_000_000_000).to_integral_value(
        rounding=ROUND_CEILING if ceiling else ROUND_FLOOR))
    if not 0 < amount <= 2**63 - 1:
        raise ValueError("USD amount is outside the supported reservation range")
    return amount


class ReservationLedger:
    """Atomic USD/call reservations shared across separate CLI invocations.

    Amounts are rounded conservatively to nanodollars. Failed requests retain
    reservations. Reopening a ledger cannot silently reset or raise its limits.
    """

    def __init__(self, path, total_usd, max_calls):
        positive_number(total_usd, "total_usd")
        positive_integer(max_calls, "max_calls")
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        total = _nanos(total_usd, ceiling=False)
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS budget_state(
                    id INTEGER PRIMARY KEY CHECK(id=1), total_nanos INTEGER NOT NULL,
                    max_calls INTEGER NOT NULL, reserved_nanos INTEGER NOT NULL DEFAULT 0,
                    calls INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS reservations(
                    reservation_id TEXT PRIMARY KEY, amount_nanos INTEGER NOT NULL,
                    model TEXT, created_at REAL NOT NULL
                );
            """)
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM budget_state WHERE id=1").fetchone()
            if row is None:
                connection.execute("INSERT INTO budget_state(id,total_nanos,max_calls) VALUES(1,?,?)", (total, max_calls))
            elif row["total_nanos"] != total or row["max_calls"] != max_calls:
                raise ValueError("Existing ledger limits differ; reuse its original limits or choose a new campaign ledger")

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def reserve(self, estimated_usd, model=None):
        positive_number(estimated_usd, "estimated_usd")
        amount = _nanos(estimated_usd, ceiling=True)
        if model is not None and (not isinstance(model, str) or len(model) > 256):
            raise ValueError("model must be a short identifier")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM budget_state WHERE id=1").fetchone()
            if row["calls"] >= row["max_calls"] or row["reserved_nanos"] + amount > row["total_nanos"]:
                raise BudgetExhausted("Shared evaluation reservation limit reached; no request was started")
            identifier = str(uuid.uuid4())
            connection.execute("INSERT INTO reservations VALUES(?,?,?,?)", (identifier, amount, model, time.time()))
            connection.execute("UPDATE budget_state SET reserved_nanos=reserved_nanos+?,calls=calls+1 WHERE id=1", (amount,))
        return {"reservation_id": identifier, "reserved_estimated_usd": amount / 1_000_000_000}

    def snapshot(self):
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM budget_state WHERE id=1").fetchone()
        return {"total_usd": row["total_nanos"] / 1_000_000_000, "max_calls": row["max_calls"],
                "calls": row["calls"], "reserved_calls": row["calls"],
                "reserved_estimated_usd": row["reserved_nanos"] / 1_000_000_000,
                "remaining_calls": row["max_calls"] - row["calls"],
                "remaining_estimated_usd": (row["total_nanos"] - row["reserved_nanos"]) / 1_000_000_000,
                "accounting": "conservative reservations, not actual provider charges"}
