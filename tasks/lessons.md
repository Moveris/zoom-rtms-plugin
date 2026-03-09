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

### No explicit camera on/off events in RTMS SDK
The Zoom RTMS SDK has no `onVideoMute`, `onCameraOff`, or similar event. Camera-off = silence of `onVideoData` callbacks. Camera-on = chunks resume. Detect camera toggles by tracking `lastChunkTime` per participant and checking for gaps (5s+) when chunks resume for a `done` participant.

### RTMS allows only one active session per meeting
`startRTMS()` fails if already running (e.g., sidebar was refreshed). Wrap in try/catch and continue to scanning view — the server session may still be active.

### Host identity: string vs numeric ID
Sidebar JWT `uid` is a string, RTMS `userId` is numeric. Compare as `String(rtmsUserId) === excludeUserId`.

## Architecture Patterns

### `retryParticipant()` is a powerful primitive
Deleting from the participants map resets the `state.done` gate in `onH264Chunk()` and lets the next H.264 chunk create a fresh batch decoder. Reuse this for any re-scan mechanism (manual retry, background re-scan, "scan all now").

## Process

### Always update README when pushing to main
Every push to main should include README updates for any new features, changed behavior, or new configuration. Don't let documentation drift from the code — update the README in the same commit or immediately after.

## Deployment

### Google Drive FUSE paths cause Docker/Fly deploy timeouts
Never run `docker build` or `fly deploy` from a Google Drive FUSE-mounted path. Clone to a local temp directory (e.g., `/tmp/`) first — FUSE latency causes context upload timeouts.

## Frame Timestamps & Model Configuration

### Don't use `Date.now()` for individual frame timestamps
**Date:** 2026-03-09
**Severity:** Critical — likely cause of unreliable liveness scores

Frame timestamps must reflect real temporal spacing (~100ms for 10 FPS). Using `Date.now()` per-frame produces nearly identical timestamps when frames are decoded in quick succession from an H.264 batch. The liveness model needs temporal progression between frames. Reconstruct from the batch's start time: `baseTimestamp + (index * 100)`.

### Hybrid-v2-30 model requires 30 frames — don't use with 10-frame batches
**Date:** 2026-03-09
**Severity:** High — produced unreliable results

The `hybrid-v2-30` model expects 30 frames. Sending only 10 frames with `model: "hybrid-v2-30"` produces unreliable results. If switching models, verify the frame count matches. The default 10-frame model requires no explicit `model` parameter (SDK defaults to `"10"`).

### Always check that FRAME_COUNT matches the model
**Date:** 2026-03-09
**Severity:** High — silent mismatch causes bad scores

When reverting a model change, also revert the frame count. The 30-frame switch changed both `FRAME_COUNT=30` and `model="hybrid-v2-30"` across two files (`h264-batch-decoder.ts` and `orchestrator.ts`). Reverting only one creates a mismatch. Always grep for ALL references to the model/frame-count before changing either.
