# Lessons Learned — Zoom RTMS Plugin

## Moveris Liveness Model Requirements

### Aspect ratio matters for liveness
16:9 → 4:3 stretching causes false "fake" verdicts. Always use `force_original_aspect_ratio=decrease` + padding in FFmpeg scale filters to preserve original aspect ratio.

### Zoom post-processing kills liveness
Portrait lighting, touch-up appearance, and virtual backgrounds all cause false "fake" verdicts. Document this prominently in user-facing guidance. HD video with effects disabled is required for reliable results.

### HD video is required
Low-resolution video produces unreliable liveness results. Ensure RTMS is configured for HD (1280x720) resolution.

### Frames must be consecutive
Moveris liveness model requires temporal continuity. Never skip, sample every Nth, or "evenly space" frames. If decoding 120 frames from 4s of video, take 10 consecutive frames (e.g., frames 55-64), not every 12th.

## Zoom RTMS SDK

### RTMS allows only one active session per meeting
`startRTMS()` fails if already running (e.g., sidebar was refreshed). Wrap in try/catch and continue to scanning view — the server session may still be active.

### Host identity: string vs numeric ID
Sidebar JWT `uid` is a string, RTMS `userId` is numeric. Compare as `String(rtmsUserId) === excludeUserId`.

## Architecture Patterns

### `retryParticipant()` is a powerful primitive
Deleting from the participants map resets the `state.done` gate in `onH264Chunk()` and lets the next H.264 chunk create a fresh batch decoder. Reuse this for any re-scan mechanism (manual retry, background re-scan, "scan all now").

## Deployment

### Google Drive FUSE paths cause Docker/Fly deploy timeouts
Never run `docker build` or `fly deploy` from a Google Drive FUSE-mounted path. Clone to a local temp directory (e.g., `/tmp/`) first — FUSE latency causes context upload timeouts.
