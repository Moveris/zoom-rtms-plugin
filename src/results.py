from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, UTC
import asyncio
from typing import Any


@dataclass
class FaceFrame:
    """A single cropped, quality-filtered face ready for submission to Moveris."""

    participant_id: str
    frame_index: int
    # Base64-encoded 224x224 PNG, no data URI prefix.
    # Bounding box expanded to 3x face size per fast-check-crops requirements.
    image_b64: str


@dataclass
class LivenessResult:
    """Result of a Moveris liveness check for one participant in one meeting."""

    meeting_uuid: str
    participant_id: str
    verdict: str  # "live" | "fake" | "error"
    score: float  # 0–100 display score from Moveris `score` field
    real_score: float  # 0–1 raw liveness probability
    fake_score: float  # 0–1 spoof probability
    confidence: float  # 0–1
    processing_ms: int
    frames_processed: int
    session_id: str  # UUID echoed from Moveris response
    completed_at: datetime
    error: str | None = None

    @property
    def passed(self) -> bool:
        """True if the participant passed the liveness threshold (score >= 65)."""
        return self.verdict == "live"


@dataclass
class SessionStatus:
    """Status of a full meeting session, containing results per participant."""

    meeting_uuid: str
    state: str  # "pending" | "processing" | "complete" | "error"
    participants: dict[str, Any] = field(
        default_factory=dict
    )  # str -> LivenessResult | None
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    completed_at: datetime | None = None


class ResultStore(ABC):
    @abstractmethod
    async def create_session(self, meeting_uuid: str) -> None: ...

    @abstractmethod
    async def set_result(
        self, meeting_uuid: str, participant_id: str, result: LivenessResult
    ) -> None: ...

    @abstractmethod
    async def get_session(self, meeting_uuid: str) -> SessionStatus | None: ...

    @abstractmethod
    async def set_session_state(self, meeting_uuid: str, state: str) -> None: ...

    @abstractmethod
    async def cleanup_session(self, meeting_uuid: str) -> None: ...


class InMemoryResultStore(ResultStore):
    def __init__(self) -> None:
        self._sessions: dict[str, SessionStatus] = {}
        self._lock = asyncio.Lock()

    async def create_session(self, meeting_uuid: str) -> None:
        async with self._lock:
            if meeting_uuid not in self._sessions:
                self._sessions[meeting_uuid] = SessionStatus(
                    meeting_uuid=meeting_uuid,
                    state="pending",
                )

    async def set_result(
        self, meeting_uuid: str, participant_id: str, result: LivenessResult
    ) -> None:
        async with self._lock:
            session = self._sessions.get(meeting_uuid)
            if session is not None:
                session.participants[participant_id] = result

    async def get_session(self, meeting_uuid: str) -> SessionStatus | None:
        return self._sessions.get(meeting_uuid)

    async def set_session_state(self, meeting_uuid: str, state: str) -> None:
        async with self._lock:
            session = self._sessions.get(meeting_uuid)
            if session is not None:
                session.state = state
                if state in ("complete", "error"):
                    session.completed_at = datetime.now(UTC)

    async def cleanup_session(self, meeting_uuid: str) -> None:
        async with self._lock:
            self._sessions.pop(meeting_uuid, None)
