"""FastAPI application exposing the AIS decoding and vessel snapshot endpoints."""
from __future__ import annotations

import os
import threading
from datetime import datetime, timezone
from typing import Literal, Optional

from fastapi import FastAPI, HTTPException
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
    MessageData,
    ReviewBatch,
    ReviewBatchSummary,
    ReviewCreateRequest,
    ReviewPool,
    ReviewPoolCandidate,
    ReviewResolution,
    ReviewResolveRequest,
    SentenceIn,
    Summary,
    VesselSnapshot,
)
from .nmea import Fragment, SentenceError, parse_sentence
from .storage import (
    KIND_POSITION,
    KIND_STATIC,
    SnapshotStore,
    SnapshotWrite,
    normalize_observed_at,
    parse_observed_at,
)

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
        "GET /vessels. Ambiguous batches can be parked via POST /reviews "
        "and adjudicated later via POST /reviews/{batch_id}/resolve."
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


def _summarize(total_sentences: int, results: list[DecodeResult]) -> Summary:
    return Summary(
        total_sentences=total_sentences,
        invalid_sentences=sum(1 for r in results if r.status == "error" and r.error != "decode_error"),
        decoded=sum(1 for r in results if r.status == "decoded"),
        incomplete=sum(1 for r in results if r.status == "incomplete"),
        ambiguous=sum(1 for r in results if r.status == "ambiguous"),
        errors=sum(1 for r in results if r.status == "error"),
    )


def _build_response(items: list[SentenceIn]) -> DecodeResponse:
    results = process_batch(items)
    return DecodeResponse(
        summary=_summarize(len(items), results), results=results
    )


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


# ---------------------------------------------------------------------------
# Manual review of ambiguous batches
# ---------------------------------------------------------------------------

def _review_batch_response(record: dict) -> ReviewBatch:
    """Rebuild the API view of a stored review batch row."""
    payload = record["payload"]
    resolution = record["resolution"]
    return ReviewBatch(
        batch_id=record["batch_id"],
        status=record["status"],
        observed_at=parse_observed_at(record["observed_at"]),
        created_at=parse_observed_at(record["created_at"]),
        resolved_at=(
            parse_observed_at(record["resolved_at"]) if record["resolved_at"] else None
        ),
        sentences=[SentenceIn(**s) for s in payload["sentences"]],
        summary=Summary(**payload["summary"]),
        results=[DecodeResult(**r) for r in payload["results"]],
        pools=[ReviewPool(**p) for p in payload["pools"]],
        resolution=ReviewResolution(**resolution) if resolution is not None else None,
    )


def _message_kind(message: MessageData) -> str:
    return KIND_POSITION if message.message_type in (1, 2, 3) else KIND_STATIC


@app.post(
    "/reviews",
    status_code=201,
    response_model=ReviewBatch,
    responses={
        201: {"description": "Pending review batch created; snapshots untouched."},
        422: {"description": "The batch contains no ambiguous fragment pool."},
    },
    tags=["review"],
    description=(
        "Park an ambiguous batch for manual adjudication. The sentences are "
        "validated, reassembled and decoded exactly as in /decode; the "
        "original batch and every ambiguous pool's candidates are persisted "
        "and a batch id is returned. Only batches containing at least one "
        "ambiguous fragment pool are accepted, and creating the batch never "
        "modifies the vessel snapshots."
    ),
)
def create_review_batch(request: ReviewCreateRequest):
    results = process_batch(request.sentences)
    ambiguous = [r for r in results if r.status == "ambiguous"]
    if not ambiguous:
        raise HTTPException(
            status_code=422,
            detail=(
                "batch contains no ambiguous fragment pool; unambiguous "
                "batches can be written directly via /ingest"
            ),
        )
    pools = [
        ReviewPool(
            pool_id=pool_id,
            source_id=result.source_id,
            sentence_indexes=result.sentence_indexes,
            channel=result.channel,
            total_fragments=result.total_fragments,
            detail=result.detail,
            candidates=[
                ReviewPoolCandidate(candidate_id=i, **candidate.model_dump())
                for i, candidate in enumerate(result.candidates or [])
            ],
        )
        for pool_id, result in enumerate(ambiguous)
    ]
    payload = {
        "sentences": [s.model_dump() for s in request.sentences],
        "summary": _summarize(len(request.sentences), results).model_dump(),
        "results": [r.model_dump(mode="json") for r in results],
        "pools": [p.model_dump(mode="json") for p in pools],
    }
    store = get_store()
    batch_id = store.create_review_batch(
        normalize_observed_at(request.observed_at),
        normalize_observed_at(datetime.now(timezone.utc)),
        payload,
    )
    return _review_batch_response(store.get_review_batch(batch_id))


@app.post(
    "/reviews/{batch_id}/resolve",
    response_model=ReviewBatch,
    responses={
        200: {"description": "Batch adjudicated and snapshots updated atomically."},
        404: {"description": "Unknown batch id."},
        409: {"description": "Batch was already resolved."},
        422: {
            "description": (
                "Selections are missing, duplicated, refer to unlisted "
                "pools/candidates or pick a candidate that failed to decode; "
                "the whole resolution is rejected and nothing is written."
            ),
        },
    },
    tags=["review"],
    description=(
        "Adjudicate a pending batch: choose exactly one listed, successfully "
        "decoded candidate per ambiguous pool. The chosen candidates and the "
        "batch's unambiguous decoded messages are written to the snapshots "
        "in original sentence-index order (first of a vessel/class wins on "
        "equal observed_at; newer stored observations are never rolled "
        "back). The status flip and the snapshot writes commit together; a "
        "resolved batch cannot be re-resolved."
    ),
)
def resolve_review_batch(batch_id: int, request: ReviewResolveRequest):
    store = get_store()
    record = store.get_review_batch(batch_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"review batch {batch_id} not found")
    if record["status"] == "resolved":
        raise HTTPException(
            status_code=409, detail=f"review batch {batch_id} is already resolved"
        )
    batch = _review_batch_response(record)

    # Validate every selection before anything is written: each pool needs
    # exactly one listed candidate that decoded successfully.
    pools = {pool.pool_id: pool for pool in batch.pools}
    chosen: dict[int, ReviewPoolCandidate] = {}
    for selection in request.selections:
        pool_id = selection.pool_id
        if pool_id in chosen:
            raise HTTPException(
                status_code=422, detail=f"duplicate selection for pool {pool_id}"
            )
        pool = pools.get(pool_id)
        if pool is None:
            raise HTTPException(
                status_code=422,
                detail=f"batch {batch_id} has no ambiguous pool {pool_id}",
            )
        candidate = next(
            (c for c in pool.candidates if c.candidate_id == selection.candidate_id),
            None,
        )
        if candidate is None:
            raise HTTPException(
                status_code=422,
                detail=f"pool {pool_id} has no candidate {selection.candidate_id}",
            )
        if candidate.error is not None or candidate.decoded is None:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"candidate {selection.candidate_id} of pool {pool_id} did "
                    "not decode successfully and cannot be selected"
                ),
            )
        chosen[pool_id] = candidate
    missing = sorted(set(pools) - set(chosen))
    if missing:
        raise HTTPException(
            status_code=422,
            detail=(
                "missing selection for ambiguous pool(s) "
                + ", ".join(map(str, missing))
            ),
        )

    # Stage the chosen candidates and the batch's unambiguous decodes in
    # original sentence-index order; errors and incomplete groups are
    # never written.
    staged: list[tuple[int, str, MessageData]] = []
    for result in batch.results:
        if result.status == "decoded" and result.decoded is not None:
            staged.append((result.sentence_indexes[0], result.source_id, result.decoded))
    for pool_id, candidate in chosen.items():
        staged.append(
            (candidate.sentence_indexes[0], pools[pool_id].source_id, candidate.decoded)
        )
    staged.sort(key=lambda item: item[0])

    observed_at = record["observed_at"]
    writes: list[SnapshotWrite] = []
    involved: set[tuple[str, int]] = set()
    for _index, source_id, message in staged:
        writes.append(
            (source_id, message.mmsi, _message_kind(message), observed_at,
             message.model_dump())
        )
        involved.add((source_id, message.mmsi))

    resolved_at = normalize_observed_at(datetime.now(timezone.utc))
    resolution = {
        "selections": [s.model_dump() for s in request.selections],
        "resolved_at": resolved_at,
    }
    snapshots = store.resolve_review_batch(
        batch_id, resolved_at, writes, sorted(involved), resolution
    )
    if snapshots is None:  # lost a race against a concurrent resolution
        raise HTTPException(
            status_code=409, detail=f"review batch {batch_id} is already resolved"
        )
    return _review_batch_response(store.get_review_batch(batch_id))


@app.get(
    "/reviews",
    response_model=list[ReviewBatchSummary],
    tags=["review"],
    description=(
        "List review batches (optionally filtered to 'pending' or "
        "'resolved'), oldest first. Use GET /reviews/{batch_id} for the "
        "sentences, candidates and resolution of one batch."
    ),
)
def list_review_batches(status: Optional[Literal["pending", "resolved"]] = None):
    return [
        ReviewBatchSummary(
            batch_id=record["batch_id"],
            status=record["status"],
            observed_at=parse_observed_at(record["observed_at"]),
            created_at=parse_observed_at(record["created_at"]),
            resolved_at=(
                parse_observed_at(record["resolved_at"])
                if record["resolved_at"]
                else None
            ),
            total_sentences=len(record["payload"]["sentences"]),
            ambiguous_pools=len(record["payload"]["pools"]),
        )
        for record in get_store().list_review_batches(status=status)
    ]


@app.get(
    "/reviews/{batch_id}",
    response_model=ReviewBatch,
    responses={404: {"description": "Unknown batch id."}},
    tags=["review"],
    description=(
        "Fetch one review batch: original sentences, decode results, every "
        "ambiguous pool's candidates and — once adjudicated — the recorded "
        "resolution with the snapshots that were actually stored."
    ),
)
def get_review_batch(batch_id: int):
    record = get_store().get_review_batch(batch_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"review batch {batch_id} not found")
    return _review_batch_response(record)
