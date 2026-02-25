"""Unit tests for src/moveris/client.py."""

from unittest.mock import patch

import httpx
import pytest
import respx

from src.moveris.client import (
    REQUIRED_FRAMES,
    MoverisClient,
    MoverisError,
    MoverisResponse,
    _BASE_URL,
)

_CROPS = ["fake_b64"] * REQUIRED_FRAMES

_LIVE_PAYLOAD = {
    "verdict": "live",
    "score": 85.0,
    "real_score": 0.85,
    "fake_score": 0.15,
    "confidence": 0.92,
}

_FAKE_PAYLOAD = {
    "verdict": "fake",
    "score": 30.0,
    "real_score": 0.30,
    "fake_score": 0.70,
    "confidence": 0.88,
}

_CROPS_URL = f"{_BASE_URL}/api/v1/fast-check-crops"


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


async def test_check_crops_returns_live_response():
    with respx.mock() as mock:
        mock.post(_CROPS_URL).mock(return_value=httpx.Response(200, json=_LIVE_PAYLOAD))
        async with MoverisClient(api_key="sk-test") as client:
            result = await client.check_crops(_CROPS)

    assert isinstance(result, MoverisResponse)
    assert result.verdict == "live"
    assert result.score == 85.0
    assert result.real_score == 0.85
    assert result.fake_score == 0.15
    assert result.confidence == 0.92


async def test_check_crops_returns_fake_response():
    with respx.mock() as mock:
        mock.post(_CROPS_URL).mock(return_value=httpx.Response(200, json=_FAKE_PAYLOAD))
        async with MoverisClient(api_key="sk-test") as client:
            result = await client.check_crops(_CROPS)

    assert result.verdict == "fake"


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------


async def test_check_crops_wrong_count_raises():
    async with MoverisClient(api_key="sk-test") as client:
        with pytest.raises(ValueError, match="exactly 10"):
            await client.check_crops(["a"] * 5)


# ---------------------------------------------------------------------------
# Non-retryable errors
# ---------------------------------------------------------------------------


async def test_check_crops_invalid_key_raises_moveris_error():
    with respx.mock() as mock:
        mock.post(_CROPS_URL).mock(
            return_value=httpx.Response(401, json={"detail": "invalid_key"})
        )
        async with MoverisClient(api_key="bad") as client:
            with pytest.raises(MoverisError) as exc_info:
                await client.check_crops(_CROPS)

    assert exc_info.value.status_code == 401


async def test_check_crops_no_credits_raises_moveris_error():
    with respx.mock() as mock:
        mock.post(_CROPS_URL).mock(
            return_value=httpx.Response(402, json={"detail": "insufficient_credits"})
        )
        async with MoverisClient(api_key="sk-test") as client:
            with pytest.raises(MoverisError) as exc_info:
                await client.check_crops(_CROPS)

    assert exc_info.value.status_code == 402


async def test_check_crops_validation_error_raises_moveris_error():
    with respx.mock() as mock:
        mock.post(_CROPS_URL).mock(
            return_value=httpx.Response(422, json={"detail": "validation_error"})
        )
        async with MoverisClient(api_key="sk-test") as client:
            with pytest.raises(MoverisError) as exc_info:
                await client.check_crops(_CROPS)

    assert exc_info.value.status_code == 422


# ---------------------------------------------------------------------------
# Retryable errors
# ---------------------------------------------------------------------------


async def test_check_crops_retries_on_500_and_succeeds():
    call_count = 0
    responses = [
        httpx.Response(500, json={"detail": "internal_error"}),
        httpx.Response(500, json={"detail": "internal_error"}),
        httpx.Response(200, json=_LIVE_PAYLOAD),
    ]

    def handler(request):
        nonlocal call_count
        resp = responses[call_count]
        call_count += 1
        return resp

    with respx.mock() as mock:
        mock.post(_CROPS_URL).mock(side_effect=handler)
        async with MoverisClient(api_key="sk-test") as client:
            with patch("src.moveris.client.asyncio.sleep"):
                result = await client.check_crops(_CROPS)

    assert result.verdict == "live"
    assert call_count == 3


async def test_check_crops_exhausts_retries_raises():
    with respx.mock() as mock:
        mock.post(_CROPS_URL).mock(
            return_value=httpx.Response(500, json={"detail": "internal_error"})
        )
        async with MoverisClient(api_key="sk-test") as client:
            with patch("src.moveris.client.asyncio.sleep"):
                with pytest.raises(httpx.HTTPStatusError):
                    await client.check_crops(_CROPS)


async def test_check_crops_rate_limited_uses_retry_after():
    call_count = 0
    responses = [
        httpx.Response(429, json={"detail": "rate_limit_exceeded", "retry_after": 0.5}),
        httpx.Response(200, json=_LIVE_PAYLOAD),
    ]

    def handler(request):
        nonlocal call_count
        resp = responses[call_count]
        call_count += 1
        return resp

    with respx.mock() as mock:
        mock.post(_CROPS_URL).mock(side_effect=handler)
        async with MoverisClient(api_key="sk-test") as client:
            with patch("src.moveris.client.asyncio.sleep") as mock_sleep:
                result = await client.check_crops(_CROPS)

    assert result.verdict == "live"
    mock_sleep.assert_called_once_with(0.5)
