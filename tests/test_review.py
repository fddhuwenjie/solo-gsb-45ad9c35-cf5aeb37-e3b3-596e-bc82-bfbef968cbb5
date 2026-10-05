"""Integration tests for the manual review endpoints (/reviews)."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import app.main as main
from tests.conftest import (
    AMBIG_F1A,
    AMBIG_F1B,
    AMBIG_F2,
    INCOMPLETE_F1,
    SINGLE_FRAGMENT,
    make_sentence,
)

T1 = "2026-10-05T11:00:00Z"
T2 = "2026-10-05T12:00:00Z"
T3 = "2026-10-05T13:00:00Z"

MMSI = 366999999
NORTHERN_STAR = 366000001
SOUTHERN_CROSS = 366000002

# Same MMSI as SINGLE_FRAGMENT but a different longitude.
OTHER_POSITION = make_sentence("AIVDM,1,1,,A,15Mwqgh01DreF9HGD@44gkg40000,0")

# Fragment 1 of 2 whose combination with AMBIG_F2 cannot decode (the joined
# payload holds 304 bits, a type-1 message requires exactly 168).
BAD_FRAG1 = make_sentence("AIVDM,2,1,,B,15Mwqgh01DreE9HGD@44gkg40000,0")


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """TestClient backed by a fresh snapshot database per test."""
    monkeypatch.setenv("AIS_DB_PATH", str(tmp_path / "snapshots.db"))
    monkeypatch.setattr(main, "_store", None)
    yield TestClient(main.app)
    monkeypatch.setattr(main, "_store", None)


def one(source_id: str, sentence: str) -> dict:
    return {"source_id": source_id, "sentence": sentence}


def create(client: TestClient, sentences: list[dict], observed_at: str = T2):
    response = client.post(
        "/reviews", json={"observed_at": observed_at, "sentences": sentences}
    )
    return response.status_code, response.json()


def resolve(client: TestClient, batch_id: int, selections: list[dict]):
    response = client.post(
        f"/reviews/{batch_id}/resolve", json={"selections": selections}
    )
    return response.status_code, response.json()


def ambiguous_batch(source_id: str = "rx-2") -> list[dict]:
    return [one(source_id, AMBIG_F1A), one(source_id, AMBIG_F1B), one(source_id, AMBIG_F2)]


# -- creation -----------------------------------------------------------------


def test_create_review_batch_returns_id_pools_and_candidates(client):
    status, body = create(client, ambiguous_batch())
    assert status == 201
    assert body["batch_id"] >= 1
    assert body["status"] == "pending"
    assert body["observed_at"].startswith("2026-10-05T12:00:00")
    assert body["resolution"] is None
    assert body["resolved_at"] is None
    # the original sentences are stored verbatim
    assert [s["sentence"] for s in body["sentences"]] == [AMBIG_F1A, AMBIG_F1B, AMBIG_F2]
    assert body["summary"]["ambiguous"] == 1
    (pool,) = body["pools"]
    assert pool["pool_id"] == 0
    assert pool["source_id"] == "rx-2"
    assert pool["sentence_indexes"] == [0, 1, 2]
    assert [c["candidate_id"] for c in pool["candidates"]] == [0, 1]
    names = {c["decoded"]["name"] for c in pool["candidates"]}
    assert names == {"NORTHERN STAR", "SOUTHERN CROSS"}


def test_create_review_batch_does_not_touch_snapshots(client):
    status, _ = create(client, [one("rx-1", SINGLE_FRAGMENT)] + ambiguous_batch())
    assert status == 201
    assert client.get("/vessels").json() == []


def test_create_review_batch_rejects_unambiguous_batch(client):
    status, body = create(client, [one("rx-1", SINGLE_FRAGMENT)])
    assert status == 422
    assert "no ambiguous" in body["detail"]
    assert client.get("/reviews").json() == []


def test_create_review_batch_validates_like_decode(client):
    bad = SINGLE_FRAGMENT.replace("15Mwqgh", "15Mwqgi")  # checksum mismatch
    status, body = create(client, [one("rx-1", bad)] + ambiguous_batch())
    assert status == 201  # errors do not prevent parking an ambiguous batch
    statuses = {r["status"] for r in body["results"]}
    assert statuses == {"error", "ambiguous"}


# -- resolution ---------------------------------------------------------------


def test_resolve_writes_chosen_candidate_and_unambiguous_decodes(client):
    batch = [one("rx-2", SINGLE_FRAGMENT)] + ambiguous_batch()
    status, body = create(client, batch)
    assert status == 201
    batch_id = body["batch_id"]
    # candidate 1 is SOUTHERN CROSS (mmsi 366000002)
    assert body["pools"][0]["candidates"][1]["decoded"]["mmsi"] == SOUTHERN_CROSS

    status, body = resolve(client, batch_id, [{"pool_id": 0, "candidate_id": 1}])
    assert status == 200
    assert body["status"] == "resolved"
    assert body["resolved_at"] is not None
    resolution = body["resolution"]
    assert resolution["selections"] == [{"pool_id": 0, "candidate_id": 1}]
    snapshots = {s["mmsi"]: s for s in resolution["snapshots"]}
    # the unambiguous position report and the chosen candidate were stored
    assert snapshots[MMSI]["position"]["data"]["longitude"] == pytest.approx(-73.9835)
    assert snapshots[SOUTHERN_CROSS]["static"]["data"]["name"] == "SOUTHERN CROSS"
    assert snapshots[SOUTHERN_CROSS]["static"]["observed_at"].startswith(
        "2026-10-05T12:00:00"
    )
    # the rejected candidate was not stored
    assert client.get("/vessels", params={"mmsi": NORTHERN_STAR}).json() == []
    (stored,) = client.get("/vessels", params={"mmsi": SOUTHERN_CROSS}).json()
    assert stored["static"]["data"]["name"] == "SOUTHERN CROSS"


def test_resolve_multiple_pools_requires_one_selection_each(client):
    batch = ambiguous_batch("rx-2") + ambiguous_batch("rx-9")
    status, body = create(client, batch)
    assert status == 201
    batch_id = body["batch_id"]
    assert [p["pool_id"] for p in body["pools"]] == [0, 1]
    assert body["pools"][1]["source_id"] == "rx-9"

    # selecting only one pool is rejected and nothing is written
    status, body = resolve(client, batch_id, [{"pool_id": 0, "candidate_id": 0}])
    assert status == 422
    assert "pool(s) 1" in body["detail"]
    assert client.get("/vessels").json() == []
    assert client.get(f"/reviews/{batch_id}").json()["status"] == "pending"

    status, body = resolve(
        client,
        batch_id,
        [{"pool_id": 0, "candidate_id": 0}, {"pool_id": 1, "candidate_id": 1}],
    )
    assert status == 200
    stored = {
        (s["source_id"], s["mmsi"])
        for s in client.get("/vessels").json()
    }
    assert stored == {("rx-2", NORTHERN_STAR), ("rx-9", SOUTHERN_CROSS)}


def test_resolve_rejects_forged_or_duplicate_selections(client):
    status, body = create(client, ambiguous_batch())
    batch_id = body["batch_id"]

    for selections in (
        [{"pool_id": 0, "candidate_id": 99}],   # unlisted candidate
        [{"pool_id": 7, "candidate_id": 0}],    # unlisted pool
        [{"pool_id": -1, "candidate_id": 0}],   # invalid id
        [                                        # same pool twice
            {"pool_id": 0, "candidate_id": 0},
            {"pool_id": 0, "candidate_id": 1},
        ],
    ):
        status, _ = resolve(client, batch_id, selections)
        assert status == 422, selections
    assert client.get("/vessels").json() == []
    assert client.get(f"/reviews/{batch_id}").json()["status"] == "pending"


def test_resolve_rejects_candidate_that_failed_decoding(client):
    batch = [one("rx-2", AMBIG_F1A), one("rx-2", BAD_FRAG1), one("rx-2", AMBIG_F2)]
    status, body = create(client, batch)
    assert status == 201
    batch_id = body["batch_id"]
    candidates = body["pools"][0]["candidates"]
    assert len(candidates) == 2
    bad = next(c for c in candidates if c["error"] is not None)
    good = next(c for c in candidates if c["error"] is None)
    assert bad["decoded"] is None

    status, body = resolve(
        client, batch_id, [{"pool_id": 0, "candidate_id": bad["candidate_id"]}]
    )
    assert status == 422
    assert "did not decode" in body["detail"]
    assert client.get("/vessels").json() == []

    # the decodable candidate can still be chosen afterwards
    status, body = resolve(
        client, batch_id, [{"pool_id": 0, "candidate_id": good["candidate_id"]}]
    )
    assert status == 200
    (stored,) = client.get("/vessels", params={"mmsi": NORTHERN_STAR}).json()
    assert stored["static"]["data"]["name"] == "NORTHERN STAR"


def test_resolve_rejects_empty_selections(client):
    status, body = create(client, ambiguous_batch())
    batch_id = body["batch_id"]
    response = client.post(f"/reviews/{batch_id}/resolve", json={"selections": []})
    assert response.status_code == 422


def test_resolve_twice_is_rejected_and_not_written_again(client):
    status, body = create(client, ambiguous_batch())
    batch_id = body["batch_id"]
    status, _ = resolve(client, batch_id, [{"pool_id": 0, "candidate_id": 0}])
    assert status == 200

    status, body = resolve(client, batch_id, [{"pool_id": 0, "candidate_id": 1}])
    assert status == 409
    # the first resolution still stands; the second choice was never written
    (stored,) = client.get("/vessels", params={"mmsi": NORTHERN_STAR}).json()
    assert stored["static"]["data"]["name"] == "NORTHERN STAR"
    assert client.get("/vessels", params={"mmsi": SOUTHERN_CROSS}).json() == []
    assert client.get(f"/reviews/{batch_id}").json()["resolution"]["selections"] == [
        {"pool_id": 0, "candidate_id": 0}
    ]


def test_resolve_unknown_batch_returns_404(client):
    status, body = resolve(client, 424242, [{"pool_id": 0, "candidate_id": 0}])
    assert status == 404


def test_resolve_applies_same_observed_at_writes_in_sentence_index_order(client):
    # two decoded position reports for the same vessel plus an ambiguous pool:
    # the report with the lower original sentence index wins
    batch = (
        [one("rx-1", SINGLE_FRAGMENT)]
        + ambiguous_batch()
        + [one("rx-1", OTHER_POSITION)]
    )
    status, body = create(client, batch)
    batch_id = body["batch_id"]
    status, body = resolve(client, batch_id, [{"pool_id": 0, "candidate_id": 0}])
    assert status == 200
    (stored,) = client.get("/vessels", params={"source_id": "rx-1"}).json()
    assert stored["position"]["data"]["longitude"] == pytest.approx(-73.9835)

    # reversed order at a later observation time -> the other report wins
    batch = (
        [one("rx-1", OTHER_POSITION)]
        + ambiguous_batch()
        + [one("rx-1", SINGLE_FRAGMENT)]
    )
    status, body = create(client, batch, observed_at=T3)
    batch_id = body["batch_id"]
    status, _ = resolve(client, batch_id, [{"pool_id": 0, "candidate_id": 0}])
    assert status == 200
    (stored,) = client.get("/vessels", params={"source_id": "rx-1"}).json()
    assert stored["position"]["data"]["longitude"] == pytest.approx(-73.980087)


def test_resolve_does_not_roll_back_newer_snapshots(client):
    # park a batch observed at T1
    batch = [one("rx-1", SINGLE_FRAGMENT)] + ambiguous_batch()
    status, body = create(client, batch, observed_at=T1)
    batch_id = body["batch_id"]

    # a newer observation for the same vessel arrives before adjudication
    response = client.post(
        "/ingest",
        json={"observed_at": T2, "sentences": [one("rx-1", OTHER_POSITION)]},
    )
    assert response.status_code == 200

    status, body = resolve(client, batch_id, [{"pool_id": 0, "candidate_id": 0}])
    assert status == 200
    assert body["status"] == "resolved"
    # the newer snapshot was not rolled back and is what gets returned
    snapshots = {s["mmsi"]: s for s in body["resolution"]["snapshots"]}
    assert snapshots[MMSI]["position"]["observed_at"].startswith("2026-10-05T12:00:00")
    assert snapshots[MMSI]["position"]["data"]["longitude"] == pytest.approx(-73.980087)
    (stored,) = client.get("/vessels", params={"source_id": "rx-1"}).json()
    assert stored["position"]["observed_at"].startswith("2026-10-05T12:00:00")
    # the chosen candidate (no newer competitor) was stored with the batch time
    assert snapshots[NORTHERN_STAR]["static"]["observed_at"].startswith(
        "2026-10-05T11:00:00"
    )


def test_resolve_never_stores_errors_or_incomplete_groups(client):
    bad = SINGLE_FRAGMENT.replace("15Mwqgh", "15Mwqgi")  # checksum mismatch
    batch = [one("rx-1", bad), one("rx-3", INCOMPLETE_F1)] + ambiguous_batch()
    status, body = create(client, batch)
    assert status == 201
    batch_id = body["batch_id"]
    assert {r["status"] for r in body["results"]} == {"error", "incomplete", "ambiguous"}

    status, _ = resolve(client, batch_id, [{"pool_id": 0, "candidate_id": 0}])
    assert status == 200
    vessels = client.get("/vessels").json()
    assert [(s["source_id"], s["mmsi"]) for s in vessels] == [("rx-2", NORTHERN_STAR)]


# -- queries -------------------------------------------------------------------


def test_get_review_batch_returns_candidates_and_resolution(client):
    status, body = create(client, ambiguous_batch())
    batch_id = body["batch_id"]
    status, body = resolve(client, batch_id, [{"pool_id": 0, "candidate_id": 1}])
    assert status == 200

    response = client.get(f"/reviews/{batch_id}")
    assert response.status_code == 200
    fetched = response.json()
    assert fetched["status"] == "resolved"
    assert len(fetched["pools"][0]["candidates"]) == 2
    assert fetched["resolution"]["selections"] == [{"pool_id": 0, "candidate_id": 1}]
    assert {s["mmsi"] for s in fetched["resolution"]["snapshots"]} == {SOUTHERN_CROSS}


def test_get_unknown_review_batch_returns_404(client):
    assert client.get("/reviews/424242").status_code == 404


def test_list_review_batches_filters_by_status(client):
    create(client, ambiguous_batch("rx-2"))
    _, second = create(client, ambiguous_batch("rx-9"))
    resolve(client, second["batch_id"], [{"pool_id": 0, "candidate_id": 0}])

    all_batches = client.get("/reviews").json()
    assert [(b["batch_id"], b["status"]) for b in all_batches] == [
        (1, "pending"),
        (2, "resolved"),
    ]
    assert all_batches[0]["total_sentences"] == 3
    assert all_batches[0]["ambiguous_pools"] == 1

    pending = client.get("/reviews", params={"status": "pending"}).json()
    assert [b["batch_id"] for b in pending] == [1]
    resolved = client.get("/reviews", params={"status": "resolved"}).json()
    assert [b["batch_id"] for b in resolved] == [2]
    assert resolved[0]["resolved_at"] is not None

    assert client.get("/reviews", params={"status": "bogus"}).status_code == 422


def test_review_batches_survive_store_reopen(client):
    status, pending = create(client, ambiguous_batch("rx-2"))
    assert status == 201
    _, done = create(client, ambiguous_batch("rx-9"))
    resolve(client, done["batch_id"], [{"pool_id": 0, "candidate_id": 0}])

    # simulate a container restart: drop the store, the next request reopens
    # the same database file lazily
    main._store.close()
    main._store = None

    batches = client.get("/reviews").json()
    assert [(b["batch_id"], b["status"]) for b in batches] == [
        (pending["batch_id"], "pending"),
        (done["batch_id"], "resolved"),
    ]
    fetched = client.get(f"/reviews/{done['batch_id']}").json()
    assert fetched["pools"][0]["candidates"][0]["decoded"]["name"] == "NORTHERN STAR"
    assert fetched["resolution"]["selections"] == [{"pool_id": 0, "candidate_id": 0}]
    assert fetched["resolution"]["snapshots"][0]["static"]["data"]["name"] == (
        "NORTHERN STAR"
    )
    # snapshots written by the resolution survived as well
    assert client.get("/vessels", params={"mmsi": NORTHERN_STAR}).json() != []
