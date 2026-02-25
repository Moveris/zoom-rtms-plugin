"""Session orchestrator — coordinates RTMS → face detection → Moveris pipeline.

One ``SessionOrchestrator`` manages all active meeting sessions.  For each
meeting a ``_Session`` is created that:

1. Connects an ``RTMSClient`` to the Zoom RTMS stream.
2. Routes decoded video frames to per-participant asyncio queues.
3. For each participant, runs a background task that filters frames,
   detects faces with MediaPipe, accumulates 10 quality crops, then
   submits them to the Moveris ``fast-check-crops`` API.
4. Stores ``LivenessResult`` objects in the injected ``ResultStore``.
"""

import asyncio
import logging
import time
from datetime import UTC, datetime

import numpy as np

from src.config import Settings
from src.moveris.client import MoverisClient, MoverisError, MoverisResponse
from src.results import InMemoryResultStore, LivenessResult, ResultStore
from src.rtms.client import RTMSClient
from src.video.face_detector import FaceDetector
from src.video.frame_selector import is_quality_frame

logger = logging.getLogger(__name__)

_MIN_CROPS = 10  # frames required by fast-check-crops
_FRAME_TIMEOUT_S = 30.0  # max seconds to wait for the next frame
_QUEUE_MAXSIZE = 100  # drop frames if a participant queue fills up


class TooManySessions(Exception):
    """Raised when ``MAX_CONCURRENT_SESSIONS`` would be exceeded."""


class SessionOrchestrator:
    """Top-level coordinator for all active RTMS sessions.

    Usage::

        orchestrator = SessionOrchestrator(settings, result_store)
        await orchestrator.start_session(meeting_uuid, rtms_stream_id, server_urls)
        # … frames processed automatically …
        await orchestrator.stop_session(meeting_uuid)
        await orchestrator.close()  # shut down all remaining sessions
    """

    def __init__(
        self,
        settings: Settings,
        result_store: ResultStore | None = None,
    ) -> None:
        self._settings = settings
        self._result_store: ResultStore = result_store or InMemoryResultStore()
        self._sessions: dict[str, _Session] = {}
        self._lock = asyncio.Lock()

    @property
    def active_session_count(self) -> int:
        return len(self._sessions)

    @property
    def result_store(self) -> ResultStore:
        return self._result_store

    async def start_session(
        self,
        meeting_uuid: str,
        rtms_stream_id: str,
        server_urls: str,
    ) -> None:
        """Begin streaming and liveness analysis for a meeting.

        Raises:
            TooManySessions: If ``MAX_CONCURRENT_SESSIONS`` would be exceeded.
        """
        async with self._lock:
            if len(self._sessions) >= self._settings.max_concurrent_sessions:
                raise TooManySessions(
                    f"Cannot start session {meeting_uuid}: "
                    f"max {self._settings.max_concurrent_sessions} concurrent sessions"
                )
            if meeting_uuid in self._sessions:
                logger.warning(
                    "Session already active — ignoring duplicate: %s", meeting_uuid
                )
                return

            await self._result_store.create_session(meeting_uuid)
            await self._result_store.set_session_state(meeting_uuid, "processing")

            session = _Session(
                meeting_uuid=meeting_uuid,
                rtms_stream_id=rtms_stream_id,
                server_urls=server_urls,
                settings=self._settings,
                result_store=self._result_store,
            )
            self._sessions[meeting_uuid] = session

        await session.start()
        logger.info("Session started: meeting=%s", meeting_uuid)

    async def stop_session(self, meeting_uuid: str) -> None:
        """Gracefully stop an active session and mark it complete."""
        async with self._lock:
            session = self._sessions.pop(meeting_uuid, None)

        if session is None:
            logger.debug("stop_session called for unknown meeting: %s", meeting_uuid)
            return

        await session.close()
        await self._result_store.set_session_state(meeting_uuid, "complete")
        logger.info("Session stopped: meeting=%s", meeting_uuid)

    async def close(self) -> None:
        """Stop all active sessions."""
        async with self._lock:
            sessions = list(self._sessions.items())
            self._sessions.clear()

        for meeting_uuid, session in sessions:
            await session.close()
            await self._result_store.set_session_state(meeting_uuid, "complete")

        if sessions:
            logger.info(
                "SessionOrchestrator shut down (%d sessions closed)", len(sessions)
            )


# ---------------------------------------------------------------------------
# Internal: per-meeting session
# ---------------------------------------------------------------------------


class _Session:
    def __init__(
        self,
        meeting_uuid: str,
        rtms_stream_id: str,
        server_urls: str,
        settings: Settings,
        result_store: ResultStore,
    ) -> None:
        self._meeting_uuid = meeting_uuid
        self._settings = settings
        self._result_store = result_store
        self._participant_queues: dict[str, asyncio.Queue] = {}
        self._participant_tasks: dict[str, asyncio.Task] = {}
        self._rtms = RTMSClient(
            settings=settings,
            meeting_uuid=meeting_uuid,
            rtms_stream_id=rtms_stream_id,
            server_urls=server_urls,
            on_frame=self._on_frame,
        )

    async def start(self) -> None:
        await self._rtms.start()

    async def close(self) -> None:
        await self._rtms.close()

        for task in self._participant_tasks.values():
            task.cancel()

        if self._participant_tasks:
            await asyncio.gather(
                *self._participant_tasks.values(), return_exceptions=True
            )

        self._participant_tasks.clear()
        self._participant_queues.clear()

    async def _on_frame(
        self,
        frame: np.ndarray,
        user_id: int,
        user_name: str,
        timestamp_ms: int,
    ) -> None:
        """Route a decoded video frame to the correct participant task."""
        if user_id == 0:
            return  # SDK metadata unavailable for this frame

        participant_id = str(user_id)

        if participant_id not in self._participant_queues:
            queue: asyncio.Queue[np.ndarray | None] = asyncio.Queue(
                maxsize=_QUEUE_MAXSIZE
            )
            self._participant_queues[participant_id] = queue
            task = asyncio.create_task(
                self._process_participant(participant_id, queue),
                name=f"participant-{self._meeting_uuid[:8]}-{participant_id}",
            )
            self._participant_tasks[participant_id] = task
            logger.info(
                "Spawned participant task — meeting=%s participant=%s",
                self._meeting_uuid,
                participant_id,
            )

        queue = self._participant_queues[participant_id]
        try:
            queue.put_nowait(frame)
        except asyncio.QueueFull:
            logger.debug(
                "Frame queue full — dropping frame for participant %s", participant_id
            )

    async def _process_participant(
        self,
        participant_id: str,
        queue: asyncio.Queue,
    ) -> None:
        """Collect quality face crops and submit to Moveris."""
        detector = FaceDetector()
        crops: list[str] = []
        frames_seen = 0
        start = time.monotonic()

        try:
            async with MoverisClient(api_key=self._settings.moveris_api_key) as moveris:
                while len(crops) < _MIN_CROPS:
                    try:
                        frame = await asyncio.wait_for(
                            queue.get(), timeout=_FRAME_TIMEOUT_S
                        )
                    except asyncio.TimeoutError:
                        logger.warning(
                            "Frame timeout for participant %s in meeting %s "
                            "after %d frames seen (%d/%d crops collected)",
                            participant_id,
                            self._meeting_uuid,
                            frames_seen,
                            len(crops),
                            _MIN_CROPS,
                        )
                        break

                    if frame is None:  # sentinel from close()
                        break

                    frames_seen += 1

                    if not is_quality_frame(frame):
                        continue

                    image_b64 = detector.detect(frame)
                    if image_b64 is None:
                        continue

                    crops.append(image_b64)

                if len(crops) < _MIN_CROPS:
                    await self._store_error(
                        participant_id,
                        "insufficient_frames",
                        frames_seen,
                        start,
                    )
                    return

                await self._call_moveris(
                    moveris, participant_id, crops[:_MIN_CROPS], frames_seen, start
                )

        except asyncio.CancelledError:
            logger.debug(
                "Participant task cancelled — meeting=%s participant=%s",
                self._meeting_uuid,
                participant_id,
            )
            raise
        except Exception as exc:
            logger.exception(
                "Unexpected error processing participant %s in meeting %s: %s",
                participant_id,
                self._meeting_uuid,
                exc,
            )
            await self._store_error(participant_id, str(exc), frames_seen, start)
        finally:
            detector.close()

    async def _call_moveris(
        self,
        client: MoverisClient,
        participant_id: str,
        crops: list[str],
        frames_seen: int,
        start: float,
    ) -> None:
        try:
            response: MoverisResponse = await client.check_crops(crops)
            result = LivenessResult(
                meeting_uuid=self._meeting_uuid,
                participant_id=participant_id,
                verdict=response.verdict,
                score=response.score,
                real_score=response.real_score,
                fake_score=response.fake_score,
                confidence=response.confidence,
                processing_ms=int((time.monotonic() - start) * 1000),
                frames_processed=frames_seen,
                session_id="",
                completed_at=datetime.now(UTC),
            )
            await self._result_store.set_result(
                self._meeting_uuid, participant_id, result
            )
            logger.info(
                "Liveness result — meeting=%s participant=%s verdict=%s score=%.1f",
                self._meeting_uuid,
                participant_id,
                response.verdict,
                response.score,
            )
        except MoverisError as exc:
            logger.error(
                "Moveris API error for participant %s in meeting %s: %s",
                participant_id,
                self._meeting_uuid,
                exc,
            )
            await self._store_error(participant_id, str(exc), frames_seen, start)

    async def _store_error(
        self,
        participant_id: str,
        error: str,
        frames_seen: int,
        start: float,
    ) -> None:
        await self._result_store.set_result(
            self._meeting_uuid,
            participant_id,
            LivenessResult(
                meeting_uuid=self._meeting_uuid,
                participant_id=participant_id,
                verdict="error",
                score=0.0,
                real_score=0.0,
                fake_score=0.0,
                confidence=0.0,
                processing_ms=int((time.monotonic() - start) * 1000),
                frames_processed=frames_seen,
                session_id="",
                completed_at=datetime.now(UTC),
                error=error,
            ),
        )
