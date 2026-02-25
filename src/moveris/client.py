"""Moveris liveness detection API client.

Submits batches of face crops to ``POST /api/v1/fast-check-crops`` and
returns structured liveness results.

API reference: https://documentation.moveris.com/api-reference/fast-check-crops

Authentication: ``X-API-Key`` header.
Retry policy: up to 3 attempts with exponential back-off (1 s → 2 s).
Non-retryable errors: 401 (invalid key), 402 (no credits), 422 (validation).
"""

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_BASE_URL = "https://api.moveris.com"
_FAST_CROPS_PATH = "/api/v1/fast-check-crops"

# fast-check-crops requires exactly this many frames
REQUIRED_FRAMES = 10

_MAX_ATTEMPTS = 3
_RETRY_DELAYS = (1.0, 2.0)  # seconds between attempts 1→2 and 2→3


@dataclass
class MoverisResponse:
    """Parsed response from ``POST /api/v1/fast-check-crops``."""

    verdict: str  # "live" | "fake"
    score: float  # 0–100 display score
    real_score: float  # 0–1 liveness probability
    fake_score: float  # 0–1 spoof probability
    confidence: float  # 0–1 model confidence


class MoverisError(Exception):
    """Raised on non-retryable Moveris API errors (401 / 402 / 422)."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class MoverisClient:
    """Async HTTP client for the Moveris liveness detection API.

    Must be used as an async context manager::

        async with MoverisClient(api_key="sk-...") as client:
            result = await client.check_crops(images_b64)
    """

    def __init__(self, api_key: str, base_url: str = _BASE_URL) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._http: httpx.AsyncClient | None = None

    async def __aenter__(self) -> "MoverisClient":
        self._http = httpx.AsyncClient(
            base_url=self._base_url,
            headers={"X-API-Key": self._api_key},
            timeout=30.0,
        )
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.close()

    async def close(self) -> None:
        """Release the underlying HTTP client."""
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    async def check_crops(self, images_b64: list[str]) -> MoverisResponse:
        """Submit exactly 10 face crops and return the liveness verdict.

        Args:
            images_b64: List of 10 base64-encoded 224×224 PNG strings (no
                        data-URI prefix).  Bounding boxes must be expanded to
                        3× face size per the Moveris API docs.

        Returns:
            MoverisResponse with verdict, score, and probability fields.

        Raises:
            ValueError: If ``images_b64`` does not contain exactly 10 items.
            MoverisError: Non-retryable API error (401 / 402 / 422).
            httpx.HTTPStatusError: Unexpected HTTP error after all retries.
        """
        if len(images_b64) != REQUIRED_FRAMES:
            raise ValueError(
                f"fast-check-crops requires exactly {REQUIRED_FRAMES} frames, "
                f"got {len(images_b64)}"
            )

        payload = {"pixels": images_b64}
        last_exc: Exception | None = None

        for attempt in range(_MAX_ATTEMPTS):
            try:
                data = await self._post(_FAST_CROPS_PATH, payload)
                return _parse_response(data)
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                if status in (401, 402, 422):
                    body = _safe_json(exc.response)
                    raise MoverisError(
                        f"Moveris API error {status}: {body.get('detail', str(exc))}",
                        status_code=status,
                    ) from exc
                if status == 429:
                    body = _safe_json(exc.response)
                    wait = float(
                        body.get(
                            "retry_after",
                            _RETRY_DELAYS[min(attempt, len(_RETRY_DELAYS) - 1)],
                        )
                    )
                    logger.warning(
                        "Moveris rate-limited (attempt %d/%d) — waiting %.1fs",
                        attempt + 1,
                        _MAX_ATTEMPTS,
                        wait,
                    )
                    await asyncio.sleep(wait)
                    last_exc = exc
                    continue
                # 5xx: retryable
                last_exc = exc
                logger.warning(
                    "Moveris server error %d (attempt %d/%d)",
                    status,
                    attempt + 1,
                    _MAX_ATTEMPTS,
                )
            except (httpx.TimeoutException, httpx.ConnectError) as exc:
                last_exc = exc
                logger.warning(
                    "Moveris connection error (attempt %d/%d): %s",
                    attempt + 1,
                    _MAX_ATTEMPTS,
                    exc,
                )

            if attempt < _MAX_ATTEMPTS - 1:
                await asyncio.sleep(_RETRY_DELAYS[attempt])

        assert last_exc is not None
        raise last_exc

    async def _post(self, path: str, payload: dict) -> dict:
        assert self._http is not None, "Use MoverisClient as async context manager"
        response = await self._http.post(path, json=payload)
        response.raise_for_status()
        return response.json()


def _parse_response(data: dict) -> MoverisResponse:
    return MoverisResponse(
        verdict=str(data["verdict"]),
        score=float(data["score"]),
        real_score=float(data["real_score"]),
        fake_score=float(data["fake_score"]),
        confidence=float(data["confidence"]),
    )


def _safe_json(response: httpx.Response) -> dict:
    try:
        return response.json()
    except Exception:
        return {}
