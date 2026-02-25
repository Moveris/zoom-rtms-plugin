"""Zoom RTMS SDK client wrapper.

Bridges the thread-based ``rtms`` SDK into our asyncio pipeline.
The SDK drives its event loop via a background polling thread; video
frame callbacks are forwarded to the asyncio event loop with
``asyncio.run_coroutine_threadsafe``.

Usage::

    client = RTMSClient(settings, meeting_uuid, rtms_stream_id, server_urls, on_frame)
    await client.start()
    # frames arrive asynchronously via on_frame(frame_bgr, user_id, user_name, timestamp_ms)
    await client.close()
"""

import asyncio
import logging
import os
import threading
import time
from collections.abc import Awaitable, Callable
from typing import Any

import cv2
import numpy as np

from src.config import Settings

logger = logging.getLogger(__name__)

# Callback type: (frame_bgr, user_id, user_name, timestamp_ms)
FrameCallback = Callable[[np.ndarray, int, str, int], Awaitable[None]]


class RTMSClient:
    """Wraps the Zoom RTMS SDK with asyncio integration.

    The SDK is a compiled C extension that handles all Zoom WebSocket
    protocol (signaling handshake, media negotiation, keep-alive) internally.
    We request PNG-encoded frames so no FFmpeg decoding is required.
    """

    _POLL_INTERVAL: float = 0.01  # 10 ms between SDK polls

    def __init__(
        self,
        settings: Settings,
        meeting_uuid: str,
        rtms_stream_id: str,
        server_urls: str | list[str],
        on_frame: FrameCallback,
    ) -> None:
        self._meeting_uuid = meeting_uuid
        self._rtms_stream_id = rtms_stream_id
        # SDK join() accepts a single URL string
        if isinstance(server_urls, list):
            self._server_urls = server_urls[0] if server_urls else ""
        else:
            self._server_urls = server_urls
        self._on_frame = on_frame
        self._client_id = settings.zoom_client_id
        self._client_secret = settings.zoom_client_secret

        self._sdk: Any = None
        self._poll_thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._loop: asyncio.AbstractEventLoop | None = None

    async def start(self) -> None:
        """Connect to Zoom RTMS and begin receiving video frames."""
        import rtms  # local import so tests can mock before importing this module

        self._loop = asyncio.get_running_loop()

        # The SDK reads ZOOM_CLIENT_ID and ZOOM_CLIENT_SECRET from the process
        # environment.  Pydantic-settings reads env values into the Settings
        # object but does not write them back, so we set them explicitly here.
        os.environ.setdefault("ZOOM_CLIENT_ID", self._client_id)
        os.environ.setdefault("ZOOM_CLIENT_SECRET", self._client_secret)

        self._sdk = rtms.Client()

        # Request PNG frames: pre-decoded images, no FFmpeg required.
        # The SDK caps image-codec fps at ~5, which aligns with our
        # FRAME_SAMPLE_RATE setting anyway.
        params = rtms.VideoParams()
        params.codec = rtms.VideoCodec["PNG"]
        params.resolution = rtms.VideoResolution["HD"]
        params.dataOpt = rtms.VideoDataOption["VIDEO_SINGLE_ACTIVE_STREAM"]
        params.fps = 5
        self._sdk.setVideoParams(params)

        loop = self._loop
        meeting_uuid = self._meeting_uuid

        @self._sdk.onVideoData
        def _on_video(data: bytes, size: int, timestamp: int, metadata: Any) -> None:
            self._bridge_frame(data, timestamp, metadata, loop)

        @self._sdk.onJoinConfirm
        def _on_join(reason: Any) -> None:
            logger.info("RTMS joined — meeting=%s reason=%s", meeting_uuid, reason)

        @self._sdk.onLeave
        def _on_leave(reason: Any) -> None:
            logger.info("RTMS left — meeting=%s reason=%s", meeting_uuid, reason)

        self._sdk.join(
            meeting_uuid=self._meeting_uuid,
            rtms_stream_id=self._rtms_stream_id,
            server_urls=self._server_urls,
        )

        self._poll_thread = threading.Thread(
            target=self._poll_loop,
            name=f"rtms-poll-{self._meeting_uuid}",
            daemon=True,
        )
        self._poll_thread.start()
        logger.info("RTMSClient started — meeting=%s", self._meeting_uuid)

    async def close(self) -> None:
        """Leave the RTMS session and stop the polling thread."""
        self._stop_event.set()
        if self._poll_thread is not None:
            self._poll_thread.join(timeout=2.0)
            self._poll_thread = None
        if self._sdk is not None:
            try:
                self._sdk.leave()
            except Exception:
                pass
            self._sdk = None
        logger.info("RTMSClient closed — meeting=%s", self._meeting_uuid)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _poll_loop(self) -> None:
        """Background thread: drive the SDK's internal event loop."""
        while not self._stop_event.is_set():
            try:
                if self._sdk is not None:
                    self._sdk._poll_if_needed()
            except Exception as exc:
                logger.warning("RTMS poll error: %s", exc)
            time.sleep(self._POLL_INTERVAL)

    def _bridge_frame(
        self,
        data: bytes,
        timestamp: int,
        metadata: Any,
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        """Decode PNG bytes to BGR ndarray, then schedule the async callback."""
        try:
            arr = np.frombuffer(data, dtype=np.uint8)
            frame = cv2.imdecode(arr, cv2.IMREAD_COLOR)  # BGR
            if frame is None:
                logger.debug(
                    "RTMS: failed to decode PNG frame (meeting=%s)", self._meeting_uuid
                )
                return
            user_id: int = getattr(metadata, "userId", 0)
            user_name: str = getattr(metadata, "userName", "")
        except Exception as exc:
            logger.warning("RTMS frame decode error: %s", exc)
            return

        asyncio.run_coroutine_threadsafe(
            self._on_frame(frame, user_id, user_name, timestamp),
            loop,
        )
