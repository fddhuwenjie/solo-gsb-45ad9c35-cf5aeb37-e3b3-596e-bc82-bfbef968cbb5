"""SQLite persistence for per-vessel AIS snapshots and review batches.

One row per ``(source_id, mmsi, kind)`` where ``kind`` is ``'position'``
(message types 1/2/3) or ``'static'`` (message type 5). A stored row is
only replaced by a *strictly newer* ``observed_at``:

* older batches never roll back newer state;
* an equal ``observed_at`` keeps the row that was saved first;
* inside one batch (all messages share the batch ``observed_at``) the
  caller applies decoded messages in sentence-index order, so the first
  message of a class wins.

Whole decoded messages are stored as-is — a position report whose
coordinates are "not available" (null) is stored with null coordinates;
no fields are borrowed from older messages.

The ``review_batch`` table persists manually adjudicated ambiguous
batches: the original sentences, the decode outcome and every ambiguous
pool's candidates are stored at creation; resolving a batch flips its
status and applies the chosen candidates plus the batch's unambiguous
decodes to the snapshots in one transaction, so status and snapshot
writes either both succeed or both fail.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Optional

from .models import (
    PositionReport,
    StaticAndVoyageData,
    TimedPositionReport,
    TimedStaticAndVoyageData,
    VesselSnapshot,
)

KIND_POSITION = "position"
KIND_STATIC = "static"

# One snapshot write: (source_id, mmsi, kind, observed_at, data).
SnapshotWrite = tuple[str, int, str, str, dict]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS vessel_state (
    source_id   TEXT NOT NULL,
    mmsi        INTEGER NOT NULL,
    kind        TEXT NOT NULL CHECK (kind IN ('position', 'static')),
    observed_at TEXT NOT NULL,
    data        TEXT NOT NULL,
    PRIMARY KEY (source_id, mmsi, kind)
);

CREATE TABLE IF NOT EXISTS review_batch (
    batch_id    INTEGER PRIMARY KEY,
    observed_at TEXT NOT NULL,
    status      TEXT NOT NULL CHECK (status IN ('pending', 'resolved')),
    created_at  TEXT NOT NULL,
    resolved_at TEXT,
    payload     TEXT NOT NULL,
    resolution  TEXT
);
"""


def normalize_observed_at(value: datetime) -> str:
    """Fixed-width UTC ISO 8601 string; lexicographic order == time order."""
    return (
        value.astimezone(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def parse_observed_at(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


class SnapshotStore:
    """Thread-safe SQLite-backed store of the latest message per vessel."""

    def __init__(self, path: str):
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock, self._conn:
            self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- writes -------------------------------------------------------------

    def _upsert(
        self,
        source_id: str,
        mmsi: int,
        kind: str,
        observed_at: str,
        data: dict,
    ) -> bool:
        """Core upsert; caller holds the lock and the transaction."""
        payload = json.dumps(data, separators=(",", ":"), sort_keys=True)
        cursor = self._conn.execute(
            """
            INSERT INTO vessel_state (source_id, mmsi, kind, observed_at, data)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT (source_id, mmsi, kind) DO UPDATE SET
                observed_at = excluded.observed_at,
                data = excluded.data
            WHERE excluded.observed_at > vessel_state.observed_at
            """,
            (source_id, mmsi, kind, observed_at, payload),
        )
        return cursor.rowcount > 0

    def upsert_if_newer(
        self,
        source_id: str,
        mmsi: int,
        kind: str,
        observed_at: str,
        data: dict,
    ) -> bool:
        """Store *data* unless an equal-or-newer row already exists.

        *observed_at* must be normalised with :func:`normalize_observed_at`.
        Returns True when the row was (re)written.
        """
        with self._lock, self._conn:
            return self._upsert(source_id, mmsi, kind, observed_at, data)

    # -- reads ----------------------------------------------------------------

    def _rows_unlocked(
        self,
        source_id: Optional[str] = None,
        mmsi: Optional[int] = None,
    ) -> list[sqlite3.Row]:
        sql = "SELECT source_id, mmsi, kind, observed_at, data FROM vessel_state"
        clauses: list[str] = []
        params: list = []
        if source_id is not None:
            clauses.append("source_id = ?")
            params.append(source_id)
        if mmsi is not None:
            clauses.append("mmsi = ?")
            params.append(mmsi)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY source_id, mmsi, kind"
        return list(self._conn.execute(sql, params).fetchall())

    def _rows(
        self,
        source_id: Optional[str] = None,
        mmsi: Optional[int] = None,
    ) -> list[sqlite3.Row]:
        with self._lock:
            return self._rows_unlocked(source_id, mmsi)

    @staticmethod
    def _to_snapshot(
        source_id: str, mmsi: int, rows: list[sqlite3.Row]
    ) -> VesselSnapshot:
        snapshot = VesselSnapshot(source_id=source_id, mmsi=mmsi)
        for row in rows:
            observed_at = parse_observed_at(row["observed_at"])
            data = json.loads(row["data"])
            if row["kind"] == KIND_POSITION:
                snapshot.position = TimedPositionReport(
                    observed_at=observed_at, data=PositionReport(**data)
                )
            else:
                snapshot.static = TimedStaticAndVoyageData(
                    observed_at=observed_at, data=StaticAndVoyageData(**data)
                )
        return snapshot

    def get_snapshot(self, source_id: str, mmsi: int) -> Optional[VesselSnapshot]:
        rows = self._rows(source_id=source_id, mmsi=mmsi)
        if not rows:
            return None
        return self._to_snapshot(source_id, mmsi, rows)

    def list_snapshots(
        self,
        source_id: Optional[str] = None,
        mmsi: Optional[int] = None,
    ) -> list[VesselSnapshot]:
        grouped: dict[tuple[str, int], list[sqlite3.Row]] = {}
        for row in self._rows(source_id=source_id, mmsi=mmsi):
            grouped.setdefault((row["source_id"], row["mmsi"]), []).append(row)
        return [
            self._to_snapshot(src, mmsi, rows)
            for (src, mmsi), rows in sorted(grouped.items())
        ]

    # -- review batches ---------------------------------------------------------

    @staticmethod
    def _review_record(row: sqlite3.Row) -> dict:
        return {
            "batch_id": row["batch_id"],
            "observed_at": row["observed_at"],
            "status": row["status"],
            "created_at": row["created_at"],
            "resolved_at": row["resolved_at"],
            "payload": json.loads(row["payload"]),
            "resolution": json.loads(row["resolution"]) if row["resolution"] else None,
        }

    def create_review_batch(
        self, observed_at: str, created_at: str, payload: dict
    ) -> int:
        """Persist a pending review batch and return its batch id.

        *payload* holds the original sentences, the decode summary/results
        and the ambiguous pools with their candidates (JSON-serialisable).
        """
        blob = json.dumps(payload, separators=(",", ":"), sort_keys=True)
        with self._lock, self._conn:
            cursor = self._conn.execute(
                "INSERT INTO review_batch (observed_at, status, created_at, payload) "
                "VALUES (?, 'pending', ?, ?)",
                (observed_at, created_at, blob),
            )
            return int(cursor.lastrowid)

    def get_review_batch(self, batch_id: int) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM review_batch WHERE batch_id = ?", (batch_id,)
            ).fetchone()
        return self._review_record(row) if row is not None else None

    def list_review_batches(self, status: Optional[str] = None) -> list[dict]:
        sql = "SELECT * FROM review_batch"
        params: list = []
        if status is not None:
            sql += " WHERE status = ?"
            params.append(status)
        sql += " ORDER BY batch_id"
        with self._lock:
            rows = list(self._conn.execute(sql, params).fetchall())
        return [self._review_record(row) for row in rows]

    def resolve_review_batch(
        self,
        batch_id: int,
        resolved_at: str,
        writes: list[SnapshotWrite],
        involved: list[tuple[str, int]],
        resolution: dict,
    ) -> Optional[list[VesselSnapshot]]:
        """Atomically mark a pending batch resolved and apply its writes.

        *writes* are applied in order (the caller sorts them by original
        sentence index, so the first message of a vessel/class wins on equal
        ``observed_at``); rows holding a strictly newer ``observed_at`` are
        never rolled back. The status flip, the snapshot writes and the
        stored *resolution* record commit in a single transaction — or not
        at all. Returns the *involved* vessels' snapshots as actually
        stored, or ``None`` when the batch does not exist or was already
        resolved (a resolved batch is never written twice).
        """
        with self._lock:
            try:
                cursor = self._conn.execute(
                    "UPDATE review_batch SET status = 'resolved', resolved_at = ? "
                    "WHERE batch_id = ? AND status = 'pending'",
                    (resolved_at, batch_id),
                )
                if cursor.rowcount == 0:
                    self._conn.rollback()
                    return None
                for source_id, mmsi, kind, observed_at, data in writes:
                    self._upsert(source_id, mmsi, kind, observed_at, data)
                snapshots: list[VesselSnapshot] = []
                for source_id, mmsi in involved:
                    rows = self._rows_unlocked(source_id, mmsi)
                    if rows:
                        snapshots.append(self._to_snapshot(source_id, mmsi, rows))
                record = dict(resolution)
                record["snapshots"] = [
                    s.model_dump(mode="json") for s in snapshots
                ]
                self._conn.execute(
                    "UPDATE review_batch SET resolution = ? WHERE batch_id = ?",
                    (
                        json.dumps(record, separators=(",", ":"), sort_keys=True),
                        batch_id,
                    ),
                )
                self._conn.commit()
                return snapshots
            except Exception:
                self._conn.rollback()
                raise
