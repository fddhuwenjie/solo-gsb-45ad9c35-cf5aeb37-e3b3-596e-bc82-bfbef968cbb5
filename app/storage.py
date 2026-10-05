"""SQLite persistence for per-vessel AIS snapshots and review batches.

``vessel_state``: one row per ``(source_id, mmsi, kind)`` where ``kind`` is
``'position'`` (message types 1/2/3) or ``'static'`` (message type 5). A
stored row is only replaced by a *strictly newer* ``observed_at``:

* older batches never roll back newer state;
* an equal ``observed_at`` keeps the row that was saved first;
* inside one batch (all messages share the batch ``observed_at``) the
  caller applies decoded messages in sentence-index order, so the first
  message of a class wins.

Whole decoded messages are stored as-is — a position report whose
coordinates are "not available" (null) is stored with null coordinates;
no fields are borrowed from older messages.

``review_batch``: one row per manual-review batch created from an ambiguous
batch of sentences. The original sentences and the full decode outcome
(including every candidate of every ambiguous pool) are stored as JSON, so
pending batches, their candidates and the resolution (chosen candidates plus
the snapshots actually stored) survive restarts. Resolution applies the
snapshot writes and flips the status in a single transaction.
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

CREATE TABLE IF NOT EXISTS review_batch (
    batch_id     TEXT PRIMARY KEY,
    status       TEXT NOT NULL CHECK (status IN ('pending', 'resolved')),
    observed_at  TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    resolved_at  TEXT,
    request      TEXT NOT NULL,
    results      TEXT NOT NULL,
    resolution   TEXT
);
"""

_UPSERT_SQL = """
INSERT INTO vessel_state (source_id, mmsi, kind, observed_at, data)
VALUES (?, ?, ?, ?, ?)
ON CONFLICT (source_id, mmsi, kind) DO UPDATE SET
    observed_at = excluded.observed_at,
    data = excluded.data
WHERE excluded.observed_at > vessel_state.observed_at
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

    def _upsert_row(
        self,
        source_id: str,
        mmsi: int,
        kind: str,
        observed_at: str,
        data: dict,
    ) -> bool:
        """UPSERT one row; caller must hold the lock and the transaction."""
        payload = json.dumps(data, separators=(",", ":"), sort_keys=True)
        cursor = self._conn.execute(
            _UPSERT_SQL, (source_id, mmsi, kind, observed_at, payload)
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
            return self._upsert_row(source_id, mmsi, kind, observed_at, data)

    # -- reads ----------------------------------------------------------------

    def _fetch_rows(
        self,
        source_id: Optional[str] = None,
        mmsi: Optional[int] = None,
    ) -> list[sqlite3.Row]:
        """SELECT matching ``vessel_state`` rows; caller must hold the lock."""
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
            return self._fetch_rows(source_id, mmsi)

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

    # -- review batches -------------------------------------------------------

    def create_review(
        self,
        batch_id: str,
        observed_at: str,
        created_at: str,
        request_json: str,
        results_json: str,
    ) -> None:
        """Store a new pending review batch (snapshots are not touched)."""
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO review_batch
                    (batch_id, status, observed_at, created_at, request, results)
                VALUES (?, 'pending', ?, ?, ?, ?)
                """,
                (batch_id, observed_at, created_at, request_json, results_json),
            )

    def get_review(self, batch_id: str) -> Optional[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM review_batch WHERE batch_id = ?", (batch_id,)
            ).fetchone()

    def list_reviews(self, status: Optional[str] = None) -> list[sqlite3.Row]:
        sql = "SELECT * FROM review_batch"
        params: list = []
        if status is not None:
            sql += " WHERE status = ?"
            params.append(status)
        sql += " ORDER BY created_at, batch_id"
        with self._lock:
            return list(self._conn.execute(sql, params).fetchall())

    def resolve_review(
        self,
        batch_id: str,
        resolved_at: str,
        selections: list[list[int]],
        writes: list[tuple[str, int, str, str, dict]],
    ) -> Optional[list[VesselSnapshot]]:
        """Apply snapshot *writes* and mark the batch resolved, atomically.

        *writes* are ``(source_id, mmsi, kind, observed_at, data)`` tuples in
        original-sentence-index order; each is applied with the usual
        strictly-newer rule, so newer snapshots are never rolled back. The
        status flip and every write commit or roll back together. Returns
        the involved vessels' snapshots as actually stored, or ``None`` when
        the batch is unknown or already resolved (nothing is written then).
        """
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT status FROM review_batch WHERE batch_id = ?", (batch_id,)
            ).fetchone()
            if row is None or row["status"] != "pending":
                return None
            for source_id, mmsi, kind, observed_at, data in writes:
                self._upsert_row(source_id, mmsi, kind, observed_at, data)
            involved = sorted({(w[0], w[1]) for w in writes})
            snapshots = [
                self._to_snapshot(source_id, mmsi, self._fetch_rows(source_id, mmsi))
                for source_id, mmsi in involved
            ]
            resolution = json.dumps(
                {
                    "selections": selections,
                    "snapshots": [s.model_dump(mode="json") for s in snapshots],
                },
                separators=(",", ":"),
            )
            self._conn.execute(
                """
                UPDATE review_batch
                SET status = 'resolved', resolved_at = ?, resolution = ?
                WHERE batch_id = ?
                """,
                (resolved_at, resolution, batch_id),
            )
            return snapshots
