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
|                            H264 video chunks              |
|                            per participant                 |
|                                 |                         |
|                        H264BatchDecoder                   |
|                     (~4s accumulate -> ffmpeg              |
|                      batch decode -> 640x480 PNGs)        |
|                                 |                         |
|                    LivenessClient.fastCheck()              |
|                       (@moveris/shared SDK)               |
|                                 |                         |
|                           ResultStore                     |
|                                 |                         |
|          WebSocket <-- SidebarWsServer --> Sidebar UI      |
|          (real-time progress + verdicts)                   |
+-----------------------------------------------------------+
  |
  |  GET /results/{meeting_uuid}    -- REST API
  |  /sidebar                       -- In-meeting Zoom App
  v
LivenessResult: verdict=live|fake, score=0-100
```

| Step | What happens |
|------|-------------|
| 1 | Zoom fires `meeting.rtms_started` webhook. The plugin validates the signature using `rtms.createWebhookHandler()` and starts a session (or waits for sidebar-initiated start if `AUTO_START_RTMS=false`). |
| 2 | `RTMSClient` (wrapping `@zoom/rtms` SDK `Client`) joins the RTMS stream and receives raw H264 video chunks per participant at 30 FPS HD. |
| 3 | `H264BatchDecoder` accumulates ~4 seconds of H264 data per participant, then decodes the batch in a single FFmpeg invocation to raw RGB frames. |
| 4 | 10 consecutive frames are selected from the middle of the decoded batch, converted to 640x480 PNGs via `sharp`. |
| 5 | Frames are submitted to `LivenessClient.fastCheck()` from `@moveris/shared` with `source: "live"`. |
| 6 | Moveris returns a verdict (`live` / `fake`), score (0-100), and confidence. |
| 7 | Results are stored and pushed to the in-meeting sidebar via WebSocket, and available at `GET /results/{meeting_uuid}`. |

---

## In-meeting sidebar

The plugin includes a Zoom App sidebar that hosts can open during meetings:

- **API key management** — Host enters their own Moveris API key (no server-side key required)
- **On-demand scanning** — Host clicks "Start Scan" to trigger liveness checks at a specific moment
- **Real-time results** — Per-participant progress bars and liveness verdicts update live via WebSocket
- **Per-participant retry** — Rescan button on each participant card to re-run liveness analysis without restarting the full session
- **Late joiners** — Participants who join after the scan starts are automatically picked up and scanned

The sidebar uses the Zoom Apps SDK (`@zoom/appssdk`) to authenticate via encrypted Zoom app context, and communicates with the backend over JWT-secured REST and WebSocket endpoints.

---

## Zoom client settings

Certain Zoom video settings affect liveness detection accuracy. For best results:

| Setting | Recommended | Notes |
|---------|------------|-------|
| **HD video** | ON | Required. Low-resolution video may cause inaccurate results. |
| **Portrait lighting** | OFF | Synthetic lighting effects distort face geometry, causing false "fake" verdicts. |
| **Touch up my appearance** | OFF | Skin smoothing can trigger false "fake" results. |
| **Virtual backgrounds** | OFF | May interfere with face analysis (testing in progress). |
| **Video filters** | OFF | Any post-processing filter may cause false positives. |

**General rule:** Disable all Zoom video post-processing effects before running a liveness scan. The liveness model analyzes natural face characteristics — any synthetic modification to the video feed risks triggering a false "fake" verdict.

> **Note:** Systematic one-variable-at-a-time testing is ongoing. See [MOV-1001](https://linear.app/moveris/issue/MOV-1001) for the full test matrix.

---

## Known issues

**Sidebar refresh during active scan** — If the Zoom sidebar app is refreshed while a scan is running, `startRTMS()` may fail because Zoom only allows one active RTMS session per meeting. The plugin handles this gracefully — the sidebar proceeds to the scanning view and reconnects to the active server-side session via WebSocket.

---

## Quick start

### Prerequisites

- Zoom account (Business/Education/Enterprise) with RTMS enabled
- [Moveris API key](https://documentation.moveris.com/) (optional if users provide their own via sidebar)
- Node.js >= 20.3.0 (or Docker)
- FFmpeg (for H264 decoding)

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
# Optional — not needed if users provide their own key via the sidebar
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

### 5. Configure Zoom App sidebar (optional)

In Zoom Marketplace -> your General App -> **Feature** -> **Surfaces**:

- Add "In-Meeting" sidebar
- Home URL: `https://your-ngrok-url/sidebar`
- Add scope: `zoomapp:inmeeting`
- Re-authorize OAuth after scope changes

### 6. Trigger RTMS for a live meeting (dev only)

Once the Zoom OAuth flow is completed (visit the app's install URL), you can trigger RTMS for an active meeting:

```bash
curl -X POST http://localhost:8080/dev/start-rtms/{meetingId}
```

### 7. Check results

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
| `GET` | `/sidebar` | Serves the in-meeting Zoom App sidebar UI (HTML/CSS/JS). |
| `POST` | `/api/sidebar/auth` | Decrypts Zoom app context and returns a signed JWT for sidebar authentication. |
| `POST` | `/api/sidebar/api-key` | Stores a Moveris API key for the authenticated Zoom account (JWT required). |
| `GET` | `/api/sidebar/api-key/status` | Checks if a Moveris API key is configured for the authenticated account. |
| `WS` | `/ws/sidebar?token=JWT` | WebSocket endpoint for real-time sidebar updates (progress, verdicts, session state). |

---

## Configuration

All settings via environment variables (or `.env` file):

| Variable | Type | Default | Description |
|----------|------|---------|-------------|
| `ZOOM_CLIENT_ID` | string | **required** | Zoom General App client ID |
| `ZOOM_CLIENT_SECRET` | string | **required** | Zoom General App client secret |
| `ZOOM_WEBHOOK_SECRET_TOKEN` | string | **required** | Webhook signature validation token (from Zoom Marketplace app settings) |
| `MOVERIS_API_KEY` | string | — | Moveris API key (`sk-...`). Optional if users provide their own via the sidebar. |
| `AUTO_START_RTMS` | bool | `true` | When `true`, RTMS sessions start automatically on webhook. When `false`, requires sidebar-initiated scan. |
| `JWT_SECRET` | string | auto-generated | Secret for signing sidebar auth JWTs. Auto-generated per process if not set. |
| `FRAME_SAMPLE_RATE` | int | `5` | Internal frame sample rate parameter. |
| `LIVENESS_THRESHOLD` | int | `65` | Minimum Moveris score to consider a participant "live" |
| `MAX_CONCURRENT_SESSIONS` | int | `50` | Max simultaneous RTMS sessions. `startSession()` throws `TooManySessions` above this limit. |
| `LOG_LEVEL` | string | `info` | Log level — also configures the `@zoom/rtms` SDK logger. Values: `error`, `warn`, `info`, `debug`, `trace` |
| `PORT` | int | `8080` | HTTP server port |

---

## Architecture

### SDKs used

This plugin is built on official SDKs — no custom protocol handling, no manual HMAC validation:

| SDK | Purpose |
|-----|---------|
| [`@zoom/rtms`](https://www.npmjs.com/package/@zoom/rtms) | RTMS stream connection, webhook handling, signature generation, session events |
| [`@zoom/appssdk`](https://www.npmjs.com/package/@zoom/appssdk) | Zoom Apps SDK for in-meeting sidebar context and authentication |
| [`@moveris/shared`](https://documentation.moveris.com/sdk/overview/) | Liveness API client, session ID generation, API key validation |

### Project structure

```
src/
  index.ts              # Entrypoint: config, SDK logger, server, WS server, graceful shutdown
  config.ts             # Zod-validated environment config
  app.ts                # Express app factory, OWASP security headers, mounts all routes
  types.ts              # ParticipantResult, SessionStatus interfaces
  orchestrator.ts       # SessionOrchestrator + per-participant H264 batch pipeline
  rtms-client.ts        # Thin wrapper around @zoom/rtms Client (H264 raw video)
  h264-batch-decoder.ts # Accumulates H264 chunks -> batch FFmpeg decode -> 10 consecutive PNGs
  api-key-store.ts      # In-memory per-account Moveris API key storage
  zoom-context.ts       # Decrypts Zoom app context (AES-256-GCM)
  sidebar-ws.ts         # JWT-authenticated WebSocket server for sidebar real-time updates
  results.ts            # ResultStore interface + InMemoryResultStore
  routes/
    webhook.ts          # POST /zoom/webhook (rtms.createWebhookHandler)
    oauth.ts            # GET /oauth/callback
    dev.ts              # POST /dev/start-rtms/:meetingId
    results.ts          # GET /results/:meetingUuid
    health.ts           # GET /health
    sidebar.ts          # Sidebar routes: auth, API key, static files
  sidebar/
    public/
      index.html        # In-meeting sidebar UI
      sidebar.css       # Sidebar styles
      sidebar.js        # Sidebar logic (Zoom Apps SDK + WebSocket client)
rollup.config.js        # Bundles sidebar JS for browser (IIFE + minified)
```

### Per-participant pipeline

For each participant detected in the RTMS video stream:

1. **Accumulate** — Raw H264 NAL units are fed into `H264BatchDecoder` for ~4 seconds
2. **Batch decode** — All accumulated H264 data is written to a temp file and decoded in a single FFmpeg invocation to raw RGB frames
3. **Select** — 10 consecutive frames are selected from the middle of the decoded batch (Moveris requires temporal continuity)
4. **Convert** — Selected frames are resized to 640x480 and encoded as PNG via `sharp`
5. **Submit** — `LivenessClient.fastCheck(frames, { sessionId, source: "live" })` sends frames for server-side face detection and liveness analysis
6. **Timeout** — If H264 data isn't accumulated within 30 seconds or no data arrives for 5 seconds, the participant is marked with an error

### Sidebar real-time flow

1. **Auth** — Sidebar loads Zoom Apps SDK, calls `getAppContext()`, POSTs encrypted context to `/api/sidebar/auth`, receives JWT
2. **Connect** — Sidebar opens WebSocket to `/ws/sidebar?token=JWT`, joins the meeting room
3. **API key** — Host enters Moveris API key, POSTs to `/api/sidebar/api-key`
4. **Start scan** — Host clicks "Start Scan", sidebar sends `start_monitoring` over WebSocket
5. **Progress** — Backend pushes `scan_progress` (seconds accumulated), `stage` updates (connected/recording/decoding/analyzing), and `participant_result` verdicts
6. **Display** — Sidebar UI updates in real-time with progress bars and verdict badges

### Error handling

- `LivenessApiError` from `@moveris/shared` is caught with code-specific logging (`invalid_key`, `insufficient_credits`, `rate_limit_exceeded`)
- RTMS join failures (`onJoinConfirm` with reason != 0) mark the session as errored
- RTMS disconnections (`onLeave`) and session stops (`onSessionUpdate`) clean up session state
- Media connection interruptions are logged via `onMediaConnectionInterrupted`
- H264 decode failures and accumulation/inactivity timeouts produce per-participant error results

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

The Dockerfile uses a multi-stage build with `node:22-slim` — builds TypeScript and bundles sidebar JS in a builder stage, then copies compiled JS + production dependencies into a minimal runtime image. The runtime stage installs `libstdc++6` from Debian Trixie (for `@zoom/rtms` native addon), `ffmpeg` (for H264 batch decoding), and `ca-certificates`.

---

## Development

```bash
# Install dependencies
npm install

# Run in dev mode (auto-reload with tsx)
npm run dev

# Build TypeScript + bundle sidebar JS
npm run build

# Start production server
npm start
```

---

## License

Apache 2.0 — see [LICENSE](LICENSE).
