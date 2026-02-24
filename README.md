# Moveris Zoom RTMS Plugin

Real-time liveness detection for Zoom meetings. Connects Zoom's Real-Time Media Streaming (RTMS) API to the [Moveris](https://moveris.com) liveness detection API so hosts can verify that participants are real humans — not deepfakes or AI-generated faces — during a live call.

[![CI](https://github.com/Moveris/zoom-rtms-plugin/actions/workflows/ci.yml/badge.svg)](https://github.com/Moveris/zoom-rtms-plugin/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.12%2B-blue.svg)](https://python.org)

---

## How it works

```
Zoom Meeting
  │
  │  (1) meeting.rtms_started webhook
  ▼
┌─────────────────────────────────────────────────────────┐
│                   zoom-rtms-plugin                       │
│                                                          │
│  Webhook Handler ──► RTMS Signaling WS ──► RTMS Media WS│
│                                                  │        │
│                                            H.264 NAL     │
│                                                  │        │
│                                           H264Decoder    │
│                                        (FFmpeg asyncio)  │
│                                                  │        │
│                                          FaceDetector    │
│                                         (MediaPipe)      │
│                                                  │        │
│                                        224×224 PNG crops │
│                                                  │        │
│                                       MoverisClient      │
│                                    (POST /fast-check-crops│
│                                                  │        │
│                                         ResultStore      │
└─────────────────────────────────────────────────────────┘
  │
  │  (2) GET /results/{meeting_uuid}
  ▼
LivenessResult: verdict=live|fake, score=0-100
```

| Step | What happens |
|------|-------------|
| 1 | Zoom fires `meeting.rtms_started` webhook → plugin validates signature and spawns async task |
| 2 | Plugin connects to RTMS signaling WebSocket → negotiates media stream URL |
| 3 | Plugin connects to RTMS media WebSocket → receives H.264 video frames per participant |
| 4 | FFmpeg (asyncio subprocess) decodes H.264 NAL units → BGR frames |
| 5 | MediaPipe detects faces → crops 224×224 PNG (3× face bounding box) |
| 6 | Plugin POSTs 10 quality-filtered crops to `POST /api/v1/fast-check-crops` |
| 7 | Moveris returns verdict: `live` / `fake`, score 0–100 |
| 8 | Results stored and available at `GET /results/{meeting_uuid}` |

---

## Quick start

### Prerequisites

- Zoom account (Business/Education/Enterprise) with RTMS enabled
- [Moveris API key](https://documentation.moveris.com/)
- Docker + Docker Compose

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

```bash
docker-compose up
```

The service starts on `http://localhost:8080`.

### 3. Expose your endpoint (local dev)

```bash
ngrok http 8080
# Copy the https://... URL
```

### 4. Configure Zoom webhook

In [Zoom Marketplace](https://marketplace.zoom.us) → your General App → **Feature** → **Event Subscriptions**:

- Endpoint URL: `https://your-ngrok-url/zoom/webhook`
- Events: `meeting.rtms_started`, `meeting.rtms_stopped`

Click **Validate** — the plugin will respond to the URL validation challenge automatically.

### 5. Run a meeting and check results

Start a Zoom meeting with RTMS enabled. After ~5 seconds of video:

```bash
curl http://localhost:8080/results/{meeting_uuid}
```

```json
{
  "meeting_uuid": "abc123",
  "state": "complete",
  "participants": {
    "12345": {
      "verdict": "live",
      "score": 87.3,
      "confidence": 0.94,
      "frames_processed": 10,
      "passed": true
    }
  }
}
```

---

## API reference

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/zoom/webhook` | Zoom webhook receiver — validates HMAC-SHA256 signature, handles `endpoint.url_validation`, `meeting.rtms_started`, `meeting.rtms_stopped` |
| `GET` | `/results/{meeting_uuid}` | Poll for session status and per-participant liveness results |
| `GET` | `/health` | Health check — returns `{"status":"ok","version":"...","active_sessions":N}` |

---

## Configuration

All settings via environment variables (or `.env` file):

| Variable | Type | Default | Description |
|----------|------|---------|-------------|
| `ZOOM_CLIENT_ID` | str | **required** | Zoom General App client ID |
| `ZOOM_CLIENT_SECRET` | str | **required** | Zoom General App client secret |
| `ZOOM_WEBHOOK_SECRET_TOKEN` | str | **required** | Webhook signature validation token |
| `MOVERIS_API_KEY` | str | **required** | Moveris API key (`sk-...`) |
| `MOVERIS_MODE` | `fast`\|`live` | `fast` | `fast` = 10 frames (~1s); `live` = 250 frames (~10s) continuous |
| `FRAME_SAMPLE_RATE` | int | `5` | Process every Nth frame from the 30fps stream |
| `LIVENESS_THRESHOLD` | int | `65` | Minimum Moveris score to pass (`verdict=live` requires score ≥ this) |
| `MAX_CONCURRENT_SESSIONS` | int | `50` | Max simultaneous RTMS sessions; returns 429 above this limit |
| `LOG_LEVEL` | str | `INFO` | Python logging level |

---

## Development

```bash
# Create virtual environment
python3.12 -m venv .venv && source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt -r requirements-dev.txt

# Run tests
pytest tests/ -v

# Lint
ruff check src/ tests/
ruff format --check src/ tests/

# Type check
mypy src/ --ignore-missing-imports
```

### Project structure

```
src/
├── config.py            # pydantic-settings Settings class
├── main.py              # FastAPI app + lifespan + routes
├── webhook_handler.py   # Zoom webhook signature validation + event dispatch
├── results.py           # ResultStore ABC, InMemoryResultStore, data models
├── rtms/
│   ├── signaling.py     # RTMS signaling WebSocket client (Phase 3)
│   ├── media.py         # RTMS media WebSocket client + JSON frame parser (Phase 6)
│   └── decoder.py       # H264Decoder — asyncio FFmpeg subprocess (Phase 4)
├── video/
│   ├── face_detector.py # MediaPipe face detection + 224×224 PNG crop (Phase 5)
│   └── frame_selector.py# Laplacian sharpness quality filter (Phase 5)
├── moveris/
│   └── client.py        # HTTP client for POST /api/v1/fast-check-crops (Phase 7)
└── orchestrator.py      # Full pipeline coordinator (Phase 8)
```

---

## Implementation status

| Phase | Description | Status |
|-------|-------------|--------|
| 1 | Repository skeleton, config, CI pipeline | ✅ Done |
| 2 | Zoom webhook handler + signature validation | ✅ Done |
| 3 | RTMS signaling WebSocket client | 🔲 Backlog |
| 4 | H.264 decoder (asyncio FFmpeg subprocess) | 🔲 Backlog |
| 5 | Face detector (MediaPipe) + sharpness filter | 🔲 Backlog |
| 6 | RTMS media WebSocket client + JSON frame parser | 🔲 Backlog |
| 7 | Moveris HTTP client (`/fast-check-crops`) | 🔲 Backlog |
| 8 | Session orchestrator | 🔲 Backlog |
| 9 | Finalize FastAPI endpoints + wire orchestrator | 🔲 Backlog |
| 10 | Docker + deployment examples | 🔲 Backlog |
| 11 | Full documentation suite | 🔲 Backlog |
| 12 | Complete test suite + v0.1.0 tag | 🔲 Backlog |

---

## Documentation

- [Configuration reference](docs/configuration.md)
- [Zoom integration guide](docs/integration-guide.md)
- [API reference](docs/api-reference.md)
- [Troubleshooting](docs/troubleshooting.md)
- [Data privacy (GDPR, CCPA, BIPA)](docs/data-privacy.md)

---

## License

Apache 2.0 — see [LICENSE](LICENSE). The Apache 2.0 license includes an explicit patent grant, which matters for biometric software.
