import {
  LivenessApiError,
  LivenessClient,
  generateSessionId,
  type CapturedFrame,
} from "@moveris/shared";
import type { Config } from "./config.js";
import type { ResultStore } from "./results.js";
import { RTMSClient } from "./rtms-client.js";
import { H264BatchDecoder } from "./h264-batch-decoder.js";
import type { ParticipantResult } from "./types.js";

/** How often to check if the batch decoder is ready (ms). */
const ACCUMULATION_CHECK_INTERVAL_MS = 250;
/** Max time to wait for H264 data before giving up (ms). */
const ACCUMULATION_TIMEOUT_MS = 30_000;

export class TooManySessions extends Error {}

interface ParticipantState {
  decoder: H264BatchDecoder;
  done: boolean;
  userName: string;
  checkInterval: ReturnType<typeof setInterval>;
  timeout: ReturnType<typeof setTimeout>;
}

/**
 * Progress callback — reports accumulation time instead of frame count.
 * framesCollected = elapsed seconds (e.g., 2.1)
 * framesNeeded = target seconds (e.g., 4.0)
 */
export type ProgressCallback = (
  meetingUuid: string,
  participantId: string,
  userName: string,
  framesCollected: number,
  framesNeeded: number,
) => void;

/** Lifecycle stage updates pushed to the sidebar. */
export type StageCallback = (
  meetingUuid: string,
  participantId: string | null,
  userName: string | null,
  stage: "connected" | "recording" | "decoding" | "analyzing",
) => void;

export type ResultCallback = (
  meetingUuid: string,
  participantId: string,
  result: ParticipantResult,
) => void;

interface PendingSession {
  meetingUuid: string;
  apiKey?: string;
  createdAt: Date;
}

export class SessionOrchestrator {
  private sessions = new Map<string, Session>();
  private pendingSessions = new Map<string, PendingSession>();
  private config: Config;
  private resultStore: ResultStore;
  private defaultLivenessClient: LivenessClient | null = null;
  onProgress: ProgressCallback | null = null;
  onStage: StageCallback | null = null;
  onResult: ResultCallback | null = null;

  constructor(config: Config, resultStore: ResultStore) {
    this.config = config;
    this.resultStore = resultStore;
    if (config.MOVERIS_API_KEY) {
      this.defaultLivenessClient = new LivenessClient({
        apiKey: config.MOVERIS_API_KEY,
        enableRetry: true,
      });
    }
  }

  get activeSessionCount(): number {
    return this.sessions.size;
  }

  registerPendingSession(meetingUuid: string, apiKey?: string): void {
    this.pendingSessions.set(meetingUuid, {
      meetingUuid,
      apiKey,
      createdAt: new Date(),
    });
    console.log(`Pending session registered: meeting=${meetingUuid}`);
  }

  hasPendingSession(meetingUuid: string): boolean {
    return this.pendingSessions.has(meetingUuid);
  }

  consumePendingSession(meetingUuid: string): string | undefined {
    const pending = this.pendingSessions.get(meetingUuid);
    if (!pending) return undefined;
    this.pendingSessions.delete(meetingUuid);
    return pending.apiKey;
  }

  startSession(meetingUuid: string, rtmsPayload: Record<string, any>, apiKey?: string): void {
    if (this.sessions.size >= this.config.MAX_CONCURRENT_SESSIONS) {
      throw new TooManySessions(
        `Cannot start session ${meetingUuid}: max ${this.config.MAX_CONCURRENT_SESSIONS} concurrent sessions`,
      );
    }
    if (this.sessions.has(meetingUuid)) {
      console.log(`Session already active — ignoring duplicate: ${meetingUuid}`);
      return;
    }

    let livenessClient: LivenessClient;
    if (apiKey) {
      livenessClient = new LivenessClient({ apiKey, enableRetry: true });
    } else if (this.defaultLivenessClient) {
      livenessClient = this.defaultLivenessClient;
    } else {
      console.error(`No API key for session ${meetingUuid} and no default key configured`);
      return;
    }

    this.resultStore.createSession(meetingUuid);
    this.resultStore.setSessionState(meetingUuid, "processing");

    const session = new Session(
      meetingUuid,
      rtmsPayload,
      this.resultStore,
      livenessClient,
      (reason: string) => this.onSessionError(meetingUuid, reason),
      this.onProgress,
      this.onStage,
      this.onResult,
    );
    this.sessions.set(meetingUuid, session);
    session.start();
    console.log(`Session started: meeting=${meetingUuid}`);
  }

  stopSession(meetingUuid: string): void {
    const session = this.sessions.get(meetingUuid);
    if (!session) return;
    this.sessions.delete(meetingUuid);
    session.close();
    this.resultStore.setSessionState(meetingUuid, "complete");
    console.log(`Session stopped: meeting=${meetingUuid}`);
  }

  close(): void {
    for (const [uuid, session] of this.sessions) {
      session.close();
      this.resultStore.setSessionState(uuid, "complete");
    }
    const count = this.sessions.size;
    this.sessions.clear();
    if (count > 0) {
      console.log(`SessionOrchestrator shut down (${count} sessions closed)`);
    }
  }

  private onSessionError(meetingUuid: string, reason: string): void {
    const session = this.sessions.get(meetingUuid);
    if (!session) return;
    this.sessions.delete(meetingUuid);
    session.close();
    this.resultStore.setSessionState(meetingUuid, "error");
    console.error(`Session error: meeting=${meetingUuid} reason=${reason}`);
  }
}

class Session {
  private rtms: RTMSClient;
  private participants = new Map<string, ParticipantState>();
  private meetingUuid: string;
  private resultStore: ResultStore;
  private livenessClient: LivenessClient;
  private onSessionError: (reason: string) => void;
  private onProgress: ProgressCallback | null;
  private onStage: StageCallback | null;
  private onResult: ResultCallback | null;

  constructor(
    meetingUuid: string,
    rtmsPayload: Record<string, any>,
    resultStore: ResultStore,
    livenessClient: LivenessClient,
    onSessionError: (reason: string) => void,
    onProgress: ProgressCallback | null,
    onStage: StageCallback | null,
    onResult: ResultCallback | null,
  ) {
    this.meetingUuid = meetingUuid;
    this.resultStore = resultStore;
    this.livenessClient = livenessClient;
    this.onSessionError = onSessionError;
    this.onProgress = onProgress;
    this.onStage = onStage;
    this.onResult = onResult;
    this.rtms = new RTMSClient(
      meetingUuid,
      rtmsPayload,
      // Raw H264 chunks — no decoding at the RTMS layer
      (h264Data, userId, userName, _timestampMs) => {
        this.onH264Chunk(h264Data, userId, userName);
      },
      () => {
        // RTMS joined — notify sidebar immediately
        this.onStage?.(this.meetingUuid, null, null, "connected");
      },
      (userId, userName) => {
        // Video data first seen for this user — show them instantly
        const participantId = String(userId);
        const displayName = userName || participantId;
        console.log(`Participant detected — meeting=${this.meetingUuid} participant=${participantId} name=${displayName}`);
        this.onStage?.(this.meetingUuid, participantId, displayName, "recording");
        // Report 0 / 4 seconds initially
        this.onProgress?.(this.meetingUuid, participantId, displayName, 0, 4);
      },
      (reason) => {
        this.onSessionError(`rtms_join_failed (reason=${reason})`);
      },
      (reason) => {
        this.onSessionError(`rtms_disconnected (reason=${reason})`);
      },
    );
  }

  start(): void {
    this.rtms.start();
  }

  close(): void {
    this.rtms.close();
    for (const [, state] of this.participants) {
      clearInterval(state.checkInterval);
      clearTimeout(state.timeout);
      if (!state.done) {
        state.decoder.cancel();
      }
    }
    this.participants.clear();
  }

  /**
   * Receives raw H264 chunks from RTMSClient.
   * Feeds them into the per-participant batch decoder.
   */
  private onH264Chunk(h264Data: Buffer, userId: number, userName: string): void {
    if (userId === 0) return;

    const participantId = String(userId);
    let state = this.participants.get(participantId);

    if (!state) {
      const displayName = userName || participantId;
      const decoder = new H264BatchDecoder();

      // Periodically check if the batch decoder has accumulated enough data
      const checkInterval = setInterval(() => {
        this.checkAccumulation(participantId);
      }, ACCUMULATION_CHECK_INTERVAL_MS);

      // Overall timeout: if we never get enough data
      const timeout = setTimeout(() => {
        this.onParticipantTimeout(participantId);
      }, ACCUMULATION_TIMEOUT_MS);

      state = {
        decoder,
        done: false,
        userName: displayName,
        checkInterval,
        timeout,
      };
      this.participants.set(participantId, state);
      console.log(`Spawned batch collector — meeting=${this.meetingUuid} participant=${participantId} name=${displayName}`);
    }

    if (state.done) return;

    // Feed raw H264 data into the batch decoder
    state.decoder.feed(h264Data);
  }

  /**
   * Called periodically to check if we've accumulated enough H264 data.
   * Pushes time-based progress updates to the sidebar.
   */
  private checkAccumulation(participantId: string): void {
    const state = this.participants.get(participantId);
    if (!state || state.done) return;

    const elapsedMs = state.decoder.getElapsedMs();
    const elapsedSec = elapsedMs / 1000;

    // Push time-based progress (seconds accumulated / 4 seconds target)
    this.onProgress?.(this.meetingUuid, participantId, state.userName, elapsedSec, 4);

    // Check if ready to decode
    if (state.decoder.isReady()) {
      state.done = true;
      clearInterval(state.checkInterval);
      clearTimeout(state.timeout);

      console.log(`Batch ready — meeting=${this.meetingUuid} participant=${participantId} (${state.decoder.getTotalBytes()} bytes accumulated)`);

      this.decodeBatch(participantId, state).catch((err) => {
        console.error(`Batch decode error for participant ${participantId}: ${err}`);
      });
      return;
    }

    // Check for inactivity timeout
    if (state.decoder.isTimedOut()) {
      state.done = true;
      clearInterval(state.checkInterval);
      clearTimeout(state.timeout);
      state.decoder.cancel();

      console.warn(`Inactivity timeout for participant ${participantId} in meeting ${this.meetingUuid}`);

      const errorResult: ParticipantResult = {
        meetingUuid: this.meetingUuid,
        participantId,
        result: null,
        completedAt: new Date(),
        error: "inactivity_timeout",
      };
      this.resultStore.setResult(this.meetingUuid, participantId, errorResult);
      this.onResult?.(this.meetingUuid, participantId, errorResult);
    }
  }

  /**
   * Decode the accumulated H264 batch for a participant,
   * then submit 10 consecutive frames to Moveris.
   */
  private async decodeBatch(participantId: string, state: ParticipantState): Promise<void> {
    // Notify sidebar: decoding
    this.onStage?.(this.meetingUuid, participantId, state.userName, "decoding");

    try {
      const { frames: pngFrames } = await state.decoder.decode();

      console.log(`Batch decoded — meeting=${this.meetingUuid} participant=${participantId} frames=${pngFrames.length}`);

      // Build CapturedFrame objects from the 10 consecutive PNG frames
      const capturedFrames: CapturedFrame[] = pngFrames.map((png, i) => ({
        index: i,
        timestampMs: Date.now(),
        pixels: png.toString("base64"),
      }));

      // Notify sidebar: analyzing (submitting to Moveris)
      this.onStage?.(this.meetingUuid, participantId, state.userName, "analyzing");

      await this.submitToMoveris(participantId, capturedFrames);
    } catch (err) {
      console.error(`Batch decode failed for participant ${participantId}: ${err}`);

      const errorResult: ParticipantResult = {
        meetingUuid: this.meetingUuid,
        participantId,
        result: null,
        completedAt: new Date(),
        error: `decode_error: ${err}`,
      };
      this.resultStore.setResult(this.meetingUuid, participantId, errorResult);
      this.onResult?.(this.meetingUuid, participantId, errorResult);
    }
  }

  private async submitToMoveris(participantId: string, frames: CapturedFrame[]): Promise<void> {
    try {
      const result = await this.livenessClient.fastCheck(frames, {
        sessionId: generateSessionId(),
        source: "live",
      });

      const participantResult: ParticipantResult = {
        meetingUuid: this.meetingUuid,
        participantId,
        result,
        completedAt: new Date(),
      };

      this.resultStore.setResult(this.meetingUuid, participantId, participantResult);
      this.onResult?.(this.meetingUuid, participantId, participantResult);
      console.log(
        `Liveness result — meeting=${this.meetingUuid} participant=${participantId} verdict=${result.verdict} score=${result.score}`,
      );
    } catch (err) {
      let errorCode = String(err);

      if (err instanceof LivenessApiError) {
        errorCode = err.code;
        if (err.code === "invalid_key") {
          console.error(`Moveris API key is invalid — check MOVERIS_API_KEY`);
        } else if (err.code === "insufficient_credits") {
          console.error(`Moveris account has insufficient credits`);
        } else if (err.code === "rate_limit_exceeded") {
          console.error(`Moveris rate limit exceeded for participant ${participantId}`);
        } else {
          console.error(`Moveris API error for participant ${participantId}: [${err.code}] ${err.message}`);
        }
      } else {
        console.error(`Moveris API error for participant ${participantId}: ${err}`);
      }

      const errorResult: ParticipantResult = {
        meetingUuid: this.meetingUuid,
        participantId,
        result: null,
        completedAt: new Date(),
        error: errorCode,
      };

      this.resultStore.setResult(this.meetingUuid, participantId, errorResult);
      this.onResult?.(this.meetingUuid, participantId, errorResult);
    }
  }

  private onParticipantTimeout(participantId: string): void {
    const state = this.participants.get(participantId);
    if (!state || state.done) return;
    state.done = true;
    clearInterval(state.checkInterval);
    state.decoder.cancel();

    console.log(
      `Accumulation timeout for participant ${participantId} in meeting ${this.meetingUuid} (${state.decoder.getTotalBytes()} bytes accumulated)`,
    );

    const errorResult: ParticipantResult = {
      meetingUuid: this.meetingUuid,
      participantId,
      result: null,
      completedAt: new Date(),
      error: "accumulation_timeout",
    };

    this.resultStore.setResult(this.meetingUuid, participantId, errorResult);
    this.onResult?.(this.meetingUuid, participantId, errorResult);
  }
}
