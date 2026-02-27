import rtms from "@zoom/rtms";
import { getConfig } from "./config.js";
import { createApp } from "./app.js";
import { SessionOrchestrator } from "./orchestrator.js";
import { InMemoryResultStore } from "./results.js";
import { InMemoryApiKeyStore } from "./api-key-store.js";
import { SidebarWsServer } from "./sidebar-ws.js";

const config = getConfig();

// The Zoom RTMS SDK reads these env vars for signature generation and webhook validation.
process.env.ZM_RTMS_CLIENT = config.ZOOM_CLIENT_ID;
process.env.ZM_RTMS_SECRET = config.ZOOM_CLIENT_SECRET;
process.env.ZOOM_WEBHOOK_SECRET_TOKEN = config.ZOOM_WEBHOOK_SECRET_TOKEN;

// Configure RTMS SDK logger to match our LOG_LEVEL.
const LOG_LEVEL_MAP: Record<string, typeof rtms.LogLevel[keyof typeof rtms.LogLevel]> = {
  error: rtms.LogLevel.ERROR,
  warn: rtms.LogLevel.WARN,
  info: rtms.LogLevel.INFO,
  debug: rtms.LogLevel.DEBUG,
  trace: rtms.LogLevel.TRACE,
};
rtms.configureLogger({
  level: rtms.LogLevel.TRACE,
  format: rtms.LogFormat.PROGRESSIVE,
  enabled: true,
});

const resultStore = new InMemoryResultStore();
const apiKeyStore = new InMemoryApiKeyStore();
const orchestrator = new SessionOrchestrator(config, resultStore);

const app = createApp(config, orchestrator, resultStore, apiKeyStore);

const server = app.listen(config.PORT, () => {
  console.log(`Listening on port ${config.PORT}`);
  console.log(`SessionOrchestrator ready (max_sessions=${config.MAX_CONCURRENT_SESSIONS})`);
  console.log(`Auto-start RTMS: ${config.AUTO_START_RTMS}`);
});

// Attach WebSocket server for sidebar real-time updates
const sidebarWs = new SidebarWsServer(server, config, apiKeyStore, orchestrator);

// Wire orchestrator callbacks to push updates via WebSocket
orchestrator.onProgress = (meetingUuid, participantId, userName, framesCollected, framesNeeded) => {
  sidebarWs.broadcast(meetingUuid, {
    type: "scan_progress",
    participantId,
    userName,
    framesCollected,
    framesNeeded,
  });
};

orchestrator.onStage = (meetingUuid, participantId, userName, stage) => {
  sidebarWs.broadcast(meetingUuid, {
    type: "stage",
    participantId,
    userName,
    stage,
  });
};

orchestrator.onResult = (meetingUuid, participantId, result) => {
  sidebarWs.broadcast(meetingUuid, {
    type: "participant_result",
    participantId,
    verdict: result.result?.verdict ?? null,
    score: result.result?.score ?? null,
    confidence: result.result?.confidence ?? null,
    error: result.error ?? null,
  });
};

function shutdown() {
  console.log("Shutting down...");
  sidebarWs.close();
  orchestrator.close();
  rtms.Client.uninitialize();
  server.close(() => {
    console.log("Server closed");
    process.exit(0);
  });
}

process.on("SIGTERM", shutdown);
process.on("SIGINT", shutdown);
