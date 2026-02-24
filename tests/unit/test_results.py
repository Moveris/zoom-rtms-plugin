"""Unit tests for InMemoryResultStore and data models."""

from datetime import datetime, UTC

import pytest

from src.results import InMemoryResultStore, LivenessResult


def make_result(
    meeting_uuid: str = "mtg-1", participant_id: str = "p1"
) -> LivenessResult:
    return LivenessResult(
        meeting_uuid=meeting_uuid,
        participant_id=participant_id,
        verdict="live",
        score=82.0,
        real_score=0.82,
        fake_score=0.18,
        confidence=0.94,
        processing_ms=210,
        frames_processed=10,
        session_id="sess-abc",
        completed_at=datetime.now(UTC),
    )


@pytest.mark.asyncio
async def test_create_and_get_session():
    store = InMemoryResultStore()
    await store.create_session("mtg-1")
    status = await store.get_session("mtg-1")
    assert status is not None
    assert status.meeting_uuid == "mtg-1"
    assert status.state == "pending"
    assert status.participants == {}


@pytest.mark.asyncio
async def test_create_session_idempotent():
    store = InMemoryResultStore()
    await store.create_session("mtg-1")
    await store.create_session("mtg-1")  # Should not raise or overwrite
    assert await store.get_session("mtg-1") is not None


@pytest.mark.asyncio
async def test_set_and_retrieve_result():
    store = InMemoryResultStore()
    await store.create_session("mtg-1")
    result = make_result()
    await store.set_result("mtg-1", "p1", result)
    status = await store.get_session("mtg-1")
    assert status.participants["p1"] is result


@pytest.mark.asyncio
async def test_get_nonexistent_session_returns_none():
    store = InMemoryResultStore()
    assert await store.get_session("does-not-exist") is None


@pytest.mark.asyncio
async def test_set_session_state_transitions():
    store = InMemoryResultStore()
    await store.create_session("mtg-1")
    await store.set_session_state("mtg-1", "processing")
    assert (await store.get_session("mtg-1")).state == "processing"

    await store.set_session_state("mtg-1", "complete")
    status = await store.get_session("mtg-1")
    assert status.state == "complete"
    assert status.completed_at is not None


@pytest.mark.asyncio
async def test_cleanup_session():
    store = InMemoryResultStore()
    await store.create_session("mtg-1")
    await store.cleanup_session("mtg-1")
    assert await store.get_session("mtg-1") is None


@pytest.mark.asyncio
async def test_cleanup_nonexistent_session_does_not_raise():
    store = InMemoryResultStore()
    await store.cleanup_session("never-existed")  # Should not raise


def test_liveness_result_passed_property():
    result = make_result()
    assert result.passed is True

    failed = make_result()
    failed.verdict = "fake"
    assert failed.passed is False
