"""SQLite persistence for per-vessel AIS snapshots.

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

_SCHEMA = """
CREATE TABLE IF NOT EXISTS vessel_state (
    source_id   TEXT NOT NULL,
    mmsi        INTEGER NOT NULL,
    kind        TEXT NOT NULL CHECK (kind IN ('position', 'static')),
    observed_at TEXT NOT NULL,
    data        TEXT NOT NULL,
    PRIMARY KEY (source_id, mmsi, kind)
);
"""


def normalize_observed_at(value: datetime) -> str:
    """Fixed-width UTC ISO 8601 string; lexicographic order == time order."""
    return (
        value.astimezone(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _parse_observed_at(text: str) -> datetime:
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
        payload = json.dumps(data, separators=(",", ":"), sort_keys=True)
        with self._lock, self._conn:
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

    # -- reads ----------------------------------------------------------------

    def _rows(
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
        with self._lock:
            return list(self._conn.execute(sql, params).fetchall())

    @staticmethod
    def _to_snapshot(
        source_id: str, mmsi: int, rows: list[sqlite3.Row]
    ) -> VesselSnapshot:
        snapshot = VesselSnapshot(source_id=source_id, mmsi=mmsi)
        for row in rows:
            observed_at = _parse_observed_at(row["observed_at"])
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
