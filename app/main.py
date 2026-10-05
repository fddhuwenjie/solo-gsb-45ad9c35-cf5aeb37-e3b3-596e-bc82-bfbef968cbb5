"""FastAPI application exposing the AIS decoding and vessel snapshot endpoints."""
from __future__ import annotations

import os
import threading
from typing import Optional

from fastapi import FastAPI
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from . import __version__
from .assembler import assemble
from .models import (
    DecodeRequest,
    DecodeResponse,
    DecodeResult,
    IngestRequest,
    IngestResponse,
    SentenceIn,
    Summary,
    VesselSnapshot,
)
from .nmea import Fragment, SentenceError, parse_sentence
from .storage import KIND_POSITION, KIND_STATIC, SnapshotStore, normalize_observed_at

# Default location of the snapshot database; docker-compose points
# AIS_DB_PATH at a named volume so snapshots survive restarts.
DEFAULT_DB_PATH = "ais_snapshots.db"

app = FastAPI(
    title="AIS NMEA 0183 Decoder",
    version=__version__,
    description=(
        "Decodes batches of raw !AIVDM/!AIVDO sentences. Validates XOR "
        "checksums, fragment numbering and fill bits, reassembles "
        "multi-fragment messages per source/channel/sequence id, enumerates "
        "complete combinations when no sequence id is present (HTTP 409 on "
        "ambiguity) and decodes AIS message types 1/2/3 and 5. POST /ingest "
        "additionally maintains persistent per-(source_id, MMSI) snapshots "
        "of the latest position and static/voyage messages, queryable via "
        "GET /vessels."
    ),
)

_store: Optional[SnapshotStore] = None
_store_lock = threading.Lock()


def get_store() -> SnapshotStore:
    """Lazily open the snapshot database (path from AIS_DB_PATH)."""
    global _store
    if _store is None:
        with _store_lock:
            if _store is None:
                _store = SnapshotStore(os.environ.get("AIS_DB_PATH", DEFAULT_DB_PATH))
    return _store


def _error_result(item: SentenceIn, index: int, exc: SentenceError) -> DecodeResult:
    return DecodeResult(
        source_id=item.source_id,
        sentence_indexes=[index],
        status="error",
        error=exc.code,
        detail=exc.detail,
    )


def process_batch(items: list[SentenceIn]) -> list[DecodeResult]:
    """Validate, group and decode one batch of sentences."""
    results: list[DecodeResult] = []
    fragments: list[Fragment] = []
    seen: dict[tuple[str, str], Fragment] = {}

    for index, item in enumerate(items):
        key = (item.source_id, item.sentence.strip())
        if key in seen:  # identical retransmission inside the same batch
            seen[key].indexes.append(index)
            continue
        try:
            fragment = parse_sentence(item.sentence, item.source_id, index)
        except SentenceError as exc:
            results.append(_error_result(item, index, exc))
            continue
        seen[key] = fragment
        fragments.append(fragment)

    results.extend(assemble(fragments))
    results.sort(key=lambda r: r.sentence_indexes[0])
    return results


def _build_response(items: list[SentenceIn]) -> DecodeResponse:
    results = process_batch(items)
    summary = Summary(
        total_sentences=len(items),
        invalid_sentences=sum(1 for r in results if r.status == "error" and r.error != "decode_error"),
        decoded=sum(1 for r in results if r.status == "decoded"),
        incomplete=sum(1 for r in results if r.status == "incomplete"),
        ambiguous=sum(1 for r in results if r.status == "ambiguous"),
        errors=sum(1 for r in results if r.status == "error"),
    )
    return DecodeResponse(summary=summary, results=results)


@app.get("/health", tags=["meta"])
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post(
    "/decode",
    response_model=DecodeResponse,
    responses={
        200: {"description": "Batch processed; no ambiguous group was found."},
        409: {
            "model": DecodeResponse,
            "description": (
                "At least one fragment pool without sequential message id "
                "allows several complete combinations; candidates are listed "
                "in the corresponding results."
            ),
        },
    },
    tags=["decode"],
)
def decode(request: DecodeRequest):
    response = _build_response(request.sentences)
    if any(r.status == "ambiguous" for r in response.results):
        return JSONResponse(status_code=409, content=jsonable_encoder(response))
    return response


def _apply_to_store(
    results: list[DecodeResult], observed_at: str, store: SnapshotStore
) -> list[VesselSnapshot]:
    """Persist decoded messages and return the involved vessels' snapshots.

    *results* is sorted by first sentence index, so when one batch carries
    several messages of the same class for a vessel, the first one wins
    (later ones share the batch ``observed_at`` and lose the strictly-newer
    comparison). Errors, incomplete groups and ambiguous candidates never
    reach the store.
    """
    involved: set[tuple[str, int]] = set()
    for result in results:
        if result.status != "decoded" or result.decoded is None:
            continue
        message = result.decoded
        kind = KIND_POSITION if message.message_type in (1, 2, 3) else KIND_STATIC
        store.upsert_if_newer(
            result.source_id, message.mmsi, kind, observed_at, message.model_dump()
        )
        involved.add((result.source_id, message.mmsi))

    snapshots: list[VesselSnapshot] = []
    for source_id, mmsi in sorted(involved):
        snapshot = store.get_snapshot(source_id, mmsi)
        if snapshot is not None:
            snapshots.append(snapshot)
    return snapshots


@app.post(
    "/ingest",
    response_model=IngestResponse,
    responses={
        200: {"description": "Batch processed and snapshots updated; no ambiguous group."},
        409: {
            "model": IngestResponse,
            "description": (
                "At least one ambiguous fragment pool; unambiguous decoded "
                "messages still updated the snapshots."
            ),
        },
    },
    tags=["snapshots"],
)
def ingest(request: IngestRequest):
    decoded = _build_response(request.sentences)
    observed_at = normalize_observed_at(request.observed_at)
    snapshots = _apply_to_store(decoded.results, observed_at, get_store())
    response = IngestResponse(
        observed_at=request.observed_at,
        summary=decoded.summary,
        results=decoded.results,
        snapshots=snapshots,
    )
    if any(r.status == "ambiguous" for r in response.results):
        return JSONResponse(status_code=409, content=jsonable_encoder(response))
    return response


@app.get(
    "/vessels",
    response_model=list[VesselSnapshot],
    tags=["snapshots"],
    description=(
        "List stored vessel snapshots, optionally filtered by source_id "
        "and/or MMSI. Each snapshot merges the latest position report "
        "(message 1/2/3) and the latest static/voyage message (type 5) "
        "kept for that (source_id, MMSI) pair."
    ),
)
def list_vessels(source_id: Optional[str] = None, mmsi: Optional[int] = None):
    return get_store().list_snapshots(source_id=source_id, mmsi=mmsi)
