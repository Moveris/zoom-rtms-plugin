import type http from "node:http";
import jwt from "jsonwebtoken";
import { WebSocketServer, WebSocket } from "ws";
import type { Config } from "./config.js";
import type { ApiKeyStore } from "./api-key-store.js";
import type { SessionOrchestrator } from "./orchestrator.js";

export interface SidebarMessage {
  type: string;
  [key: string]: unknown;
}

/** Per-connection metadata extracted from the JWT. */
interface ClientInfo {
  meetingUuid: string;
  userId: string;
}

/** Manages WebSocket connections grouped by meeting UUID. */
export class SidebarWsServer {
  private wss: WebSocketServer;
  private rooms = new Map<string, Set<WebSocket>>();
  private clientInfo = new WeakMap<WebSocket, ClientInfo>();
  private config: Config;
  private apiKeyStore: ApiKeyStore;
  private orchestrator: SessionOrchestrator;

  constructor(
    server: http.Server,
    config: Config,
    apiKeyStore: ApiKeyStore,
    orchestrator: SessionOrchestrator,
  ) {
    this.config = config;
    this.apiKeyStore = apiKeyStore;
    this.orchestrator = orchestrator;

    this.wss = new WebSocketServer({ server, path: "/ws/sidebar" });
    this.wss.on("connection", (ws, req) => this.onConnection(ws, req));
  }

  /** Send a message to all clients watching a specific meeting. */
  broadcast(meetingUuid: string, message: SidebarMessage): void {
    const room = this.rooms.get(meetingUuid);
    if (!room) return;

    const data = JSON.stringify(message);
    for (const ws of room) {
      if (ws.readyState === WebSocket.OPEN) {
        ws.send(data);
      }
    }
  }

  close(): void {
    this.wss.close();
  }

  private onConnection(ws: WebSocket, req: http.IncomingMessage): void {
    const url = new URL(req.url ?? "/", `http://${req.headers.host}`);
    const token = url.searchParams.get("token");

    if (!token) {
      ws.close(4001, "Missing token");
      return;
    }

    let payload: jwt.JwtPayload;
    try {
      payload = jwt.verify(token, this.config.JWT_SECRET) as jwt.JwtPayload;
    } catch {
      ws.close(4001, "Invalid token");
      return;
    }

    const meetingUuid = payload.mid as string;
    const userId = payload.uid as string;
    if (!meetingUuid) {
      ws.close(4002, "Token missing meeting UUID");
      return;
    }

    // Store per-connection metadata
    this.clientInfo.set(ws, { meetingUuid, userId });

    // Add to room
    if (!this.rooms.has(meetingUuid)) {
      this.rooms.set(meetingUuid, new Set());
    }
    this.rooms.get(meetingUuid)!.add(ws);

    console.log(`Sidebar WS connected — meeting=${meetingUuid} user=${userId} (${this.rooms.get(meetingUuid)!.size} clients)`);

    ws.on("message", (raw) => {
      try {
        const msg = JSON.parse(raw.toString()) as SidebarMessage;
        this.handleMessage(ws, msg);
      } catch {
        // Ignore malformed messages
      }
    });

    ws.on("close", () => {
      const room = this.rooms.get(meetingUuid);
      if (room) {
        room.delete(ws);
        if (room.size === 0) {
          this.rooms.delete(meetingUuid);
        }
      }
      console.log(`Sidebar WS disconnected — meeting=${meetingUuid}`);
    });
  }

  private handleMessage(ws: WebSocket, msg: SidebarMessage): void {
    const info = this.clientInfo.get(ws);
    if (!info) return;

    switch (msg.type) {
      case "start_monitoring": {
        // Look up the user's API key and pass it to the pending session
        const apiKey = this.apiKeyStore.get(info.userId);
        if (!apiKey) {
          ws.send(JSON.stringify({ type: "error", message: "No API key configured. Please add your Moveris API key first." }));
          return;
        }
        // Host can opt out of being scanned to save tokens
        const excludeUserId = msg.excludeSelf ? info.userId : undefined;
        this.orchestrator.registerPendingSession(info.meetingUuid, apiKey, excludeUserId);
        this.broadcast(info.meetingUuid, { type: "session_state", state: "pending" });
        console.log(`Sidebar requested start — meeting=${info.meetingUuid} user=${info.userId}${excludeUserId ? " (excluding self)" : ""}`);
        break;
      }

      case "stop_monitoring":
        this.orchestrator.stopSession(info.meetingUuid);
        this.broadcast(info.meetingUuid, { type: "session_state", state: "complete" });
        console.log(`Sidebar requested stop — meeting=${info.meetingUuid}`);
        break;

      case "retry_participant": {
        const participantId = msg.participantId as string;
        if (!participantId) break;
        const ok = this.orchestrator.retryParticipant(info.meetingUuid, participantId);
        if (ok) {
          console.log(`Sidebar requested retry — meeting=${info.meetingUuid} participant=${participantId}`);
        } else {
          ws.send(JSON.stringify({ type: "error", message: "Cannot retry — no active session for this meeting." }));
        }
        break;
      }

      default:
        break;
    }
  }
}
