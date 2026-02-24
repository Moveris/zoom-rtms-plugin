"""RTMS signaling WebSocket client.

Authenticates with Zoom RTMS using HMAC-SHA256, negotiates the media stream
URL, and maintains the keepalive loop for the lifetime of a meeting session.

Protocol reference: https://developers.zoom.us/docs/rtms/signal-connection/
Message types (msg_type integers):
  1  SIG_HANDSHAKE_REQ  — sent by us on connect
  2  SIG_HANDSHAKE_RESP — Zoom replies; contains media server URL list
  12 KEEP_ALIVE_REQ     — Zoom sends every ~30 s; we must echo back as 13
  13 KEEP_ALIVE_RESP    — our echo
"""

import asyncio
import hashlib
import hmac
import json
import logging
import random
from typing import Any

import websockets
import websockets.exceptions

from src.config import Settings

logger = logging.getLogger(__name__)


class RTMSSignalingError(Exception):
    """Raised when RTMS signaling fails unrecoverably."""


class RTMSSignalingClient:
    """Connects to the Zoom RTMS signaling WebSocket, authenticates, and
    maintains the keepalive loop until :meth:`close` is called.

    Usage::

        client = RTMSSignalingClient(settings, meeting_uuid, rtms_stream_id, server_urls)
        media_url = await client.connect()
        # ... open the RTMS media WebSocket on media_url ...
        await client.close()
    """

    _CONNECT_TIMEOUT: float = 5.0  # seconds per URL attempt
    _HANDSHAKE_TIMEOUT: float = 10.0  # seconds to wait for HANDSHAKE_RESP

    def __init__(
        self,
        settings: Settings,
        meeting_uuid: str,
        rtms_stream_id: str,
        server_urls: list[str],
    ) -> None:
        self._client_id = settings.zoom_client_id
        self._client_secret = settings.zoom_client_secret
        self._meeting_uuid = meeting_uuid
        self._rtms_stream_id = rtms_stream_id
        self._server_urls = server_urls
        self._ws: Any = None
        self._keepalive_task: asyncio.Task | None = None

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    async def connect(self) -> str:
        """Attempt each signaling URL in order and return the media WebSocket URL.

        Raises:
            RTMSSignalingError: if all URLs are exhausted or authentication fails.
        """
        last_exc: Exception = RTMSSignalingError("server_urls list is empty")

        for url in self._server_urls:
            # --- TCP + WebSocket connect (with timeout) ---
            try:
                async with asyncio.timeout(self._CONNECT_TIMEOUT):
                    ws = await websockets.connect(url)
            except Exception as exc:
                logger.warning("Signaling: cannot connect to %s — %s", url, exc)
                last_exc = exc
                continue

            # --- Handshake (over the open WS) ---
            try:
                self._ws = ws
                await self._send_handshake()
                media_url = await self._receive_handshake_resp()
                self._keepalive_task = asyncio.create_task(
                    self._keepalive_loop(),
                    name=f"rtms-keepalive-{self._meeting_uuid}",
                )
                logger.info(
                    "Signaling connected — meeting=%s media=%s",
                    self._meeting_uuid,
                    media_url,
                )
                return media_url
            except Exception as exc:
                logger.warning("Signaling handshake failed on %s — %s", url, exc)
                last_exc = exc
                try:
                    await ws.close()
                except Exception:
                    pass
                self._ws = None
                continue

        raise RTMSSignalingError(
            f"Exhausted all RTMS signaling URLs for meeting={self._meeting_uuid}: {last_exc}"
        )

    async def close(self) -> None:
        """Cancel the keepalive task and close the WebSocket connection."""
        if self._keepalive_task and not self._keepalive_task.done():
            self._keepalive_task.cancel()
            try:
                await self._keepalive_task
            except asyncio.CancelledError:
                pass

        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None

        logger.info("Signaling closed — meeting=%s", self._meeting_uuid)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _signature(self) -> str:
        """HMAC-SHA256 of ``{client_id},{meeting_uuid},{rtms_stream_id}``."""
        message = f"{self._client_id},{self._meeting_uuid},{self._rtms_stream_id}"
        return hmac.new(
            self._client_secret.encode("utf-8"),
            message.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    async def _send_handshake(self) -> None:
        """Send SIG_HANDSHAKE_REQ (msg_type 1)."""
        msg = {
            "msg_type": 1,
            "protocol_version": 1,
            "meeting_uuid": self._meeting_uuid,
            "rtms_stream_id": self._rtms_stream_id,
            "sequence": random.randint(0, 0xFFFFFFFF),
            "signature": self._signature(),
        }
        await self._ws.send(json.dumps(msg))

    async def _receive_handshake_resp(self) -> str:
        """Await SIG_HANDSHAKE_RESP (msg_type 2) and return the media URL."""
        raw = await asyncio.wait_for(self._ws.recv(), timeout=self._HANDSHAKE_TIMEOUT)
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        resp: dict = json.loads(raw)

        if resp.get("msg_type") != 2:
            raise RTMSSignalingError(
                f"Expected msg_type 2 (SIG_HANDSHAKE_RESP), got {resp.get('msg_type')}"
            )

        status_code = resp.get("status_code", -1)
        if status_code != 0:
            raise RTMSSignalingError(f"RTMS auth rejected: status_code={status_code}")

        media_urls: list[str] = resp.get("media_server", {}).get("server_urls", [])
        if not media_urls:
            raise RTMSSignalingError("Signaling response has no media server URLs")

        return media_urls[0]

    async def _keepalive_loop(self) -> None:
        """Echo KEEP_ALIVE_REQ (msg_type 12) back as KEEP_ALIVE_RESP (msg_type 13).

        Runs until the WebSocket closes or the task is cancelled.
        Zoom drops the signaling connection if it doesn't receive a response
        within ~90 seconds of sending a keepalive request.
        """
        try:
            async for raw_msg in self._ws:
                if isinstance(raw_msg, bytes):
                    raw_msg = raw_msg.decode("utf-8")
                try:
                    msg: dict = json.loads(raw_msg)
                except (ValueError, TypeError):
                    logger.warning("Signaling: non-JSON message received, ignoring")
                    continue

                msg_type = msg.get("msg_type")
                if msg_type == 12:
                    resp = {"msg_type": 13, "timestamp": msg.get("timestamp")}
                    await self._ws.send(json.dumps(resp))
                    logger.debug(
                        "Signaling: keepalive echoed (ts=%s)", msg.get("timestamp")
                    )
                else:
                    logger.debug("Signaling: unhandled msg_type=%s", msg_type)

        except websockets.exceptions.ConnectionClosed:
            logger.info("Signaling WS closed — meeting=%s", self._meeting_uuid)
        except asyncio.CancelledError:
            raise  # propagate so the task is properly cancelled
        except Exception as exc:
            logger.warning("Signaling keepalive error: %s", exc)
