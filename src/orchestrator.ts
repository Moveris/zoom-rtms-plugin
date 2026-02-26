import {
  BaseFrameCollector,
  LivenessApiError,
  LivenessClient,
  generateSessionId,
  getMinFramesForModel,
  type CapturedFrame,
} from "@moveris/shared";
import type { Config } from "./config.js";
import { isQualityFrame, resizeFrame } from "./frame-processor.js";
import type { ResultStore } from "./results.js";
import { RTMSClient } from "./rtms-client.js";
import type { ParticipantResult } from "./types.js";

const DEFAULT_MODEL = "10" as const;
const FRAME_TIMEOUT_MS = 30_000;

export class TooManySessions extends Error {}

interface ParticipantState {
  collector: BaseFrameCollector;
  framesSeen: number;
  done: boolean;
  timeout: ReturnType<typeof setTimeout>;
}

export class SessionOrchestrator {
  private sessions = new Map<string, Session>();
  private config: Config;
  private resultStore: ResultStore;
  private livenessClient: LivenessClient;

  constructor(config: Config, resultStore: ResultStore) {
    this.config = config;
    this.resultStore = resultStore;
    // baseUrl omitted — SDK defaults to DEFAULT_ENDPOINT ("https://api.moveris.com")
    this.livenessClient = new LivenessClient({
      apiKey: config.MOVERIS_API_KEY,
      enableRetry: true,
    });
  }

  get activeSessionCount(): number {
    return this.sessions.size;
  }

  startSession(meetingUuid: string, rtmsStreamId: string, serverUrls: string): void {
    if (this.sessions.size >= this.config.MAX_CONCURRENT_SESSIONS) {
      throw new TooManySessions(
        `Cannot start session ${meetingUuid}: max ${this.config.MAX_CONCURRENT_SESSIONS} concurrent sessions`,
      );
    }
    if (this.sessions.has(meetingUuid)) {
      console.log(`Session already active — ignoring duplicate: ${meetingUuid}`);
      return;
    }

    this.resultStore.createSession(meetingUuid);
    this.resultStore.setSessionState(meetingUuid, "processing");

    const session = new Session(
      meetingUuid,
      rtmsStreamId,
      serverUrls,
      this.config,
      this.resultStore,
      this.livenessClient,
      (reason: string) => this.onSessionError(meetingUuid, reason),
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
  private minFrames: number;
  private onSessionError: (reason: string) => void;

  constructor(
    meetingUuid: string,
    rtmsStreamId: string,
    serverUrls: string,
    config: Config,
    resultStore: ResultStore,
    livenessClient: LivenessClient,
    onSessionError: (reason: string) => void,
  ) {
    this.meetingUuid = meetingUuid;
    this.resultStore = resultStore;
    this.livenessClient = livenessClient;
    this.minFrames = getMinFramesForModel(DEFAULT_MODEL);
    this.onSessionError = onSessionError;
    this.rtms = new RTMSClient(
      config,
      meetingUuid,
      rtmsStreamId,
      serverUrls,
      (data, userId, _userName, timestampMs) => {
        this.onFrame(data, userId, timestampMs);
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
      clearTimeout(state.timeout);
    }
    this.participants.clear();
  }

  private onFrame(data: Buffer, userId: number, timestampMs: number): void {
    if (userId === 0) return;

    const participantId = String(userId);
    let state = this.participants.get(participantId);

    if (!state) {
      state = {
        collector: new BaseFrameCollector(this.minFrames),
        framesSeen: 0,
        done: false,
        timeout: setTimeout(() => {
          this.onParticipantTimeout(participantId);
        }, FRAME_TIMEOUT_MS),
      };
      this.participants.set(participantId, state);
      console.log(`Spawned participant pipeline — meeting=${this.meetingUuid} participant=${participantId}`);
    }

    if (state.done) return;
    state.framesSeen++;

    this.processFrame(data, timestampMs, participantId, state).catch((err) => {
      console.error(`Frame processing error: ${err}`);
    });
  }

  private async processFrame(
    data: Buffer,
    timestampMs: number,
    participantId: string,
    state: ParticipantState,
  ): Promise<void> {
    if (state.done || state.collector.isComplete()) return;

    const quality = await isQualityFrame(data);
    if (!quality) return;

    const resized = await resizeFrame(data);
    const pixels = resized.toString("base64");

    const frame: CapturedFrame = {
      index: state.collector.getNextIndex(),
      timestampMs,
      pixels,
    };
    state.collector.addFrame(frame);

    if (state.collector.isComplete()) {
      state.done = true;
      clearTimeout(state.timeout);
      await this.submitToMoveris(participantId, state);
    }
  }

  private async submitToMoveris(participantId: string, state: ParticipantState): Promise<void> {
    try {
      const frames = state.collector.getFrames();
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

      this.resultStore.setResult(this.meetingUuid, participantId, {
        meetingUuid: this.meetingUuid,
        participantId,
        result: null,
        completedAt: new Date(),
        error: errorCode,
      });
    }
  }

  private onParticipantTimeout(participantId: string): void {
    const state = this.participants.get(participantId);
    if (!state || state.done) return;
    state.done = true;

    const collected = state.collector.getCount();
    console.log(
      `Frame timeout for participant ${participantId} in meeting ${this.meetingUuid} (${collected}/${this.minFrames} frames collected)`,
    );

    this.resultStore.setResult(this.meetingUuid, participantId, {
      meetingUuid: this.meetingUuid,
      participantId,
      result: null,
      completedAt: new Date(),
      error: "insufficient_frames",
    });
  }
}
