"""H.264 decoder — asyncio FFmpeg subprocess with PPM frame output.

Architecture
------------
- FFmpeg reads raw H.264 NAL units from its stdin (streamed from the RTMS
  media WebSocket).
- FFmpeg writes decoded video as a sequence of PPM (Portable Pixmap) images
  to stdout.  PPM is self-framing: each frame carries its own width/height
  header, so the reader works at any resolution without prior configuration.
- A background ``_reader_task`` drains stdout, parses PPM headers, and
  enqueues BGR numpy arrays for the face detector.
- NAL units do NOT map 1:1 to decoded frames (B/P-frames).  The queue
  architecture decouples ``feed()`` writes from ``get_frame()`` reads.

FFmpeg command
--------------
    ffmpeg -loglevel error -f h264 -i pipe:0 -f image2pipe -vcodec ppm pipe:1
"""

import asyncio
import logging
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)


class H264Decoder:
    """Decodes a live H.264 NAL stream into BGR numpy arrays via FFmpeg.

    Usage::

        decoder = H264Decoder()
        await decoder.start()

        await decoder.feed(nal_bytes)          # call for each NAL unit
        frame = await decoder.get_frame(1.0)   # np.ndarray (H, W, 3) or None

        await decoder.close()

    Parameters
    ----------
    queue_size:
        Maximum number of decoded frames held in the internal queue before
        newer frames start being dropped.  30 frames ≈ 1 second at 30 fps.
    _cmd:
        Override the subprocess command.  Used in tests to substitute a
        lightweight mock instead of the real FFmpeg binary.
    """

    _FFMPEG_CMD: list[str] = [
        "ffmpeg",
        "-loglevel",
        "error",  # suppress informational output; errors still logged
        "-f",
        "h264",  # input format: raw H.264 bitstream
        "-i",
        "pipe:0",  # read from stdin
        "-f",
        "image2pipe",  # output format: image pipe
        "-vcodec",
        "ppm",  # PPM is self-framing (no need to know resolution)
        "pipe:1",  # write to stdout
    ]

    def __init__(
        self,
        queue_size: int = 30,
        _cmd: list[str] | None = None,
    ) -> None:
        self._cmd = _cmd or self._FFMPEG_CMD
        self._queue: asyncio.Queue[np.ndarray] = asyncio.Queue(maxsize=queue_size)
        self._process: Any = None  # asyncio.subprocess.Process
        self._reader_task: asyncio.Task | None = None

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Launch the FFmpeg subprocess and start the background frame reader."""
        self._process = await asyncio.create_subprocess_exec(
            *self._cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        self._reader_task = asyncio.create_task(
            self._read_frames(),
            name="h264-reader",
        )
        logger.info("H264Decoder started (pid=%s)", self._process.pid)

    async def feed(self, nal_data: bytes) -> None:
        """Write H.264 NAL unit bytes to FFmpeg stdin.

        Non-blocking — data goes into the OS pipe buffer.  If FFmpeg cannot
        keep up and the buffer fills, the write silently no-ops.
        """
        if not (self._process and self._process.stdin):
            return
        try:
            self._process.stdin.write(nal_data)
        except (BrokenPipeError, ConnectionResetError):
            logger.debug("H264Decoder: FFmpeg stdin already closed")

    async def get_frame(self, timeout: float = 1.0) -> "np.ndarray | None":
        """Return the next decoded BGR frame, or ``None`` if timeout expires."""
        try:
            return await asyncio.wait_for(self._queue.get(), timeout=timeout)
        except asyncio.TimeoutError:
            return None

    async def close(self) -> None:
        """Close stdin, cancel the reader task, and wait for FFmpeg to exit."""
        # Signal EOF to FFmpeg so it flushes remaining frames
        if self._process and self._process.stdin:
            try:
                self._process.stdin.close()
                await self._process.stdin.wait_closed()
            except Exception:
                pass

        # Cancel the reader task
        if self._reader_task and not self._reader_task.done():
            self._reader_task.cancel()
            try:
                await self._reader_task
            except asyncio.CancelledError:
                pass

        # Wait for FFmpeg to exit; kill if it takes too long
        if self._process:
            try:
                await asyncio.wait_for(self._process.wait(), timeout=3.0)
            except asyncio.TimeoutError:
                self._process.kill()

        logger.info("H264Decoder closed")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _read_frames(self) -> None:
        """Parse PPM frames from FFmpeg stdout and enqueue BGR arrays.

        PPM binary format (P6)::

            P6\\n
            {width} {height}\\n
            255\\n
            <width × height × 3 raw RGB bytes>

        Frames are converted from RGB (PPM convention) to BGR (OpenCV
        convention) by reversing the channel axis.
        """
        stdout = self._process.stdout
        try:
            while True:
                # --- magic number ---
                magic = (await stdout.readline()).strip()
                if not magic:
                    break  # clean EOF from FFmpeg
                if magic != b"P6":
                    logger.warning(
                        "H264Decoder: unexpected PPM magic %r — skipping", magic
                    )
                    continue

                # --- dimensions line: "width height" ---
                dim_line = (await stdout.readline()).strip()
                width, height = (int(x) for x in dim_line.split())

                # --- max-value line (always "255" for 8-bit PPM) ---
                await stdout.readline()

                # --- pixel data ---
                n_bytes = width * height * 3
                frame_bytes = await stdout.readexactly(n_bytes)

                # PPM stores RGB; reshape and flip to BGR for OpenCV compatibility
                frame = np.frombuffer(frame_bytes, dtype=np.uint8).reshape(
                    height, width, 3
                )
                bgr = frame[..., ::-1].copy()

                if not self._queue.full():
                    self._queue.put_nowait(bgr)
                else:
                    logger.debug(
                        "H264Decoder: frame dropped (queue full at %d)",
                        self._queue.maxsize,
                    )

        except asyncio.IncompleteReadError:
            logger.info("H264Decoder: FFmpeg stdout closed (EOF mid-frame)")
        except asyncio.CancelledError:
            raise  # propagate so asyncio can cancel the task cleanly
        except Exception as exc:
            logger.warning("H264Decoder: reader error — %s", exc)
