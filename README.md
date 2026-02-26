# Moveris Zoom RTMS Plugin

Real-time liveness detection for Zoom meetings. Connects Zoom's Real-Time Media Streaming (RTMS) API to the [Moveris](https://moveris.com) liveness detection API so hosts can verify that participants are real humans — not deepfakes or AI-generated faces — during a live call.

[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)
[![Node.js](https://img.shields.io/badge/node-%3E%3D20.3.0-brightgreen.svg)](https://nodejs.org)
[![TypeScript](https://img.shields.io/badge/typescript-5.7-blue.svg)](https://www.typescriptlang.org)

---

## How it works

```
Zoom Meeting
  |
  |  (1) meeting.rtms_started webhook
  v
+-----------------------------------------------------------+
|                    zoom-rtms-plugin                        |
|                                                           |
|  Webhook Handler -----> RTMSClient (@zoom/rtms SDK)       |
|  (rtms.createWebhookHandler)    |                         |
|                            PNG video frames               |
|                            per participant                 |
|                                 |                         |
|                          frame-processor                  |
|                       (sharp resize 640x480               |
|                        + blur analysis)                   |
|                                 |                         |
|                        BaseFrameCollector                 |
|                      (10 quality frames)                  |
|                                 |                         |
|                    LivenessClient.fastCheck()              |
|                       (@moveris/shared SDK)               |
|                                 |                         |
|                           ResultStore                     |
+-----------------------------------------------------------+
  |
  |  (2) GET /results/{meeting_uuid}
  v
LivenessResult: verdict=live|fake, score=0-100
```

| Step | What happens |
|------|-------------|
| 1 | Zoom fires `meeting.rtms_started` webhook. The plugin validates the signature using `rtms.createWebhookHandler()` and starts a session. |
| 2 | `RTMSClient` (wrapping `@zoom/rtms` SDK `Client`) joins the RTMS stream and receives PNG video frames per participant at 10 FPS. |
| 3 | Each frame is checked for blur using `analyzeBlur()` from `@moveris/shared`, then resized to 640x480 with `sharp`. |
| 4 | `BaseFrameCollector` from `@moveris/shared` collects 10 quality frames per participant. |
| 5 | Frames are submitted to `LivenessClient.fastCheck()` from `@moveris/shared` with `source: "live"`. |
| 6 | Moveris returns a verdict (`live` / `fake`), score (0-100), and confidence. |
| 7 | Results are stored and available at `GET /results/{meeting_uuid}`. |

---

## Quick start

### Prerequisites

- Zoom account (Business/Education/Enterprise) with RTMS enabled
- [Moveris API key](https://documentation.moveris.com/)
- Node.js >= 20.3.0 (or Docker)

### 1. Clone and configure

```bash
git clone https://github.com/Moveris/zoom-rtms-plugin.git
cd zoom-rtms-plugin
cp .env.example .env
```

Edit `.env`:

```env
ZOOM_CLIENT_ID=your_zoom_client_id
ZOOM_CLIENT_SECRET=your_zoom_client_secret
ZOOM_WEBHOOK_SECRET_TOKEN=your_webhook_verification_token
MOVERIS_API_KEY=sk-your-moveris-api-key
```

### 2. Start the service

**With Docker (recommended):**

```bash
docker compose up
```

**Without Docker:**

```bash
npm install
npm run build
npm start
```

The service starts on `http://localhost:8080`.

### 3. Expose your endpoint (local dev)

```bash
ngrok http 8080
# Copy the https://... URL
```

### 4. Configure Zoom webhook

In [Zoom Marketplace](https://marketplace.zoom.us) -> your General App -> **Feature** -> **Event Subscriptions**:

- Endpoint URL: `https://your-ngrok-url/zoom/webhook`
- Events: `meeting.rtms_started`, `meeting.rtms_stopped`

Click **Validate** — the plugin responds to URL validation challenges automatically via the `@zoom/rtms` SDK webhook handler.

### 5. Trigger RTMS for a live meeting (dev only)

Once the Zoom OAuth flow is completed (visit the app's install URL), you can trigger RTMS for an active meeting:

```bash
curl -X POST http://localhost:8080/dev/start-rtms/{meetingId}
```

### 6. Check results

After ~5 seconds of video per participant:

```bash
curl http://localhost:8080/results/{meeting_uuid}
```

```json
{
  "meetingUuid": "abc123",
  "state": "complete",
  "participants": {
    "12345": {
      "meetingUuid": "abc123",
      "participantId": "12345",
      "result": {
        "verdict": "live",
        "score": 87,
        "confidence": 94,
        "sessionId": "uuid-here",
        "processingMs": 320,
        "framesProcessed": 10
      },
      "completedAt": "2026-02-25T12:00:00.000Z"
    }
  },
  "startedAt": "2026-02-25T11:59:50.000Z",
  "completedAt": "2026-02-25T12:00:01.000Z"
}
```

---

## API reference

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/zoom/webhook` | Zoom webhook receiver. Uses `rtms.createWebhookHandler()` for URL validation and signature verification. Dispatches `meeting.rtms_started` and `meeting.rtms_stopped` events. |
| `GET` | `/results/{meeting_uuid}` | Poll for session status and per-participant liveness results. |
| `GET` | `/health` | Health check — returns `{"status":"ok","version":"0.1.0","active_sessions":N,"zoom_token":"present|missing"}` |
| `GET` | `/oauth/callback` | Zoom OAuth callback — exchanges authorization code for access token. |
| `POST` | `/dev/start-rtms/{meetingId}` | Dev-only — triggers RTMS for an active meeting via the Zoom REST API. Requires a valid OAuth token. |

---

## Configuration

All settings via environment variables (or `.env` file):

| Variable | Type | Default | Description |
|----------|------|---------|-------------|
| `ZOOM_CLIENT_ID` | string | **required** | Zoom General App client ID |
| `ZOOM_CLIENT_SECRET` | string | **required** | Zoom General App client secret |
| `ZOOM_WEBHOOK_SECRET_TOKEN` | string | **required** | Webhook signature validation token (from Zoom Marketplace app settings) |
| `MOVERIS_API_KEY` | string | **required** | Moveris API key (`sk-...`) |
| `FRAME_SAMPLE_RATE` | int | `10` | Video FPS requested from RTMS. Moveris recommends 10 FPS for natural signal capture. |
| `LIVENESS_THRESHOLD` | int | `65` | Minimum Moveris score to consider a participant "live" |
| `MAX_CONCURRENT_SESSIONS` | int | `50` | Max simultaneous RTMS sessions. `startSession()` throws `TooManySessions` above this limit. |
| `LOG_LEVEL` | string | `info` | Log level — also configures the `@zoom/rtms` SDK logger. Values: `error`, `warn`, `info`, `debug`, `trace` |
| `PORT` | int | `8080` | HTTP server port |

---

## Architecture

### SDKs used

This plugin is built on two official SDKs — no custom protocol handling, no manual HMAC validation, no face detection pipeline:

| SDK | Purpose |
|-----|---------|
| [`@zoom/rtms`](https://www.npmjs.com/package/@zoom/rtms) | RTMS stream connection, webhook handling, signature generation, session events |
| [`@moveris/shared`](https://documentation.moveris.com/sdk/overview/) | Liveness API client, frame collection, blur analysis, session ID generation |

### Project structure

```
src/
  index.ts              # Entrypoint: config, SDK logger, server, graceful shutdown
  config.ts             # Zod-validated environment config
  app.ts                # Express app factory, mounts all routes
  types.ts              # ParticipantResult, SessionStatus interfaces
  orchestrator.ts       # SessionOrchestrator + per-participant frame pipeline
  rtms-client.ts        # Thin wrapper around @zoom/rtms Client
  frame-processor.ts    # sharp resize + @moveris/shared blur analysis
  results.ts            # ResultStore interface + InMemoryResultStore
  routes/
    webhook.ts          # POST /zoom/webhook (rtms.createWebhookHandler)
    oauth.ts            # GET /oauth/callback
    dev.ts              # POST /dev/start-rtms/:meetingId
    results.ts          # GET /results/:meetingUuid
    health.ts           # GET /health
```

### Per-participant pipeline

For each participant detected in the RTMS video stream:

1. **Blur check** — `analyzeBlur()` + `rgbaToGrayscale()` from `@moveris/shared` reject blurry frames
2. **Resize** — `sharp` resizes to 640x480 PNG
3. **Collect** — `BaseFrameCollector` from `@moveris/shared` buffers frames until 10 quality frames are captured
4. **Submit** — `LivenessClient.fastCheck(frames, { sessionId, source: "live" })` sends frames for server-side face detection and liveness analysis
5. **Timeout** — If 10 frames aren't collected within 30 seconds, the participant is marked with `error: "insufficient_frames"`

### Error handling

- `LivenessApiError` from `@moveris/shared` is caught with code-specific logging (`invalid_key`, `insufficient_credits`, `rate_limit_exceeded`)
- RTMS join failures (`onJoinConfirm` with reason != 0) mark the session as errored
- RTMS disconnections (`onLeave`) and session stops (`onSessionUpdate`) clean up session state
- Media connection interruptions are logged via `onMediaConnectionInterrupted`

---

## Deployment

### Fly.io

```bash
# First time
fly apps create zoom-rtms-plugin-staging --org moveris

# Set secrets
fly secrets set \
  ZOOM_CLIENT_ID=... \
  ZOOM_CLIENT_SECRET=... \
  ZOOM_WEBHOOK_SECRET_TOKEN=... \
  MOVERIS_API_KEY=sk-... \
  --app zoom-rtms-plugin-staging

# Deploy
fly deploy --config fly.staging.toml
```

### Docker

```bash
docker compose up -d
```

The Dockerfile uses a multi-stage build with `node:22-slim` — builds TypeScript in a builder stage, then copies compiled JS + production dependencies into a minimal runtime image. The runtime stage installs `libstdc++6` from Debian Trixie to satisfy the `@zoom/rtms` native addon's GLIBCXX requirement.

---

## Development

```bash
# Install dependencies
npm install

# Run in dev mode (auto-reload with tsx)
npm run dev

# Build TypeScript
npm run build

# Start production server
npm start
```

---

## License

Apache 2.0 — see [LICENSE](LICENSE).
