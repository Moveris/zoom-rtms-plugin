import rtms from "@zoom/rtms";
import type { Metadata, SessionInfo } from "@zoom/rtms";
import type { Config } from "./config.js";

export type FrameCallback = (
  data: Buffer,
  userId: number,
  userName: string,
  timestampMs: number,
) => void;

export type SessionErrorCallback = (reason: number) => void;

export class RTMSClient {
  private sdk: InstanceType<typeof rtms.Client> | null = null;
  private config: Config;
  private meetingUuid: string;
  private rtmsStreamId: string;
  private serverUrls: string;
  private onFrame: FrameCallback;
  private onError: SessionErrorCallback;
  private onDisconnect: SessionErrorCallback;

  constructor(
    config: Config,
    meetingUuid: string,
    rtmsStreamId: string,
    serverUrls: string,
    onFrame: FrameCallback,
    onError: SessionErrorCallback,
    onDisconnect: SessionErrorCallback,
  ) {
    this.config = config;
    this.meetingUuid = meetingUuid;
    this.rtmsStreamId = rtmsStreamId;
    this.serverUrls = serverUrls;
    this.onFrame = onFrame;
    this.onError = onError;
    this.onDisconnect = onDisconnect;
  }

  start(): void {
    this.sdk = new rtms.Client();

    // 1. Register callbacks first (per SDK quickstart recommended order)
    this.sdk.onVideoData((data: Buffer, size: number, timestamp: number, metadata: Metadata) => {
      this.onFrame(data.subarray(0, size), metadata.userId, metadata.userName, timestamp);
    });

    this.sdk.onJoinConfirm((reason: number) => {
      if (reason === 0) {
        console.log(`RTMS joined — meeting=${this.meetingUuid}`);
      } else {
        console.error(`RTMS join failed — meeting=${this.meetingUuid} reason=${reason}`);
        this.onError(reason);
      }
    });

    this.sdk.onLeave((reason: number) => {
      console.log(`RTMS left — meeting=${this.meetingUuid} reason=${reason}`);
      this.onDisconnect(reason);
    });

    this.sdk.onSessionUpdate((op: number, sessionInfo: SessionInfo) => {
      if (op === rtms.SESSION_EVENT_STOP) {
        console.log(`RTMS session stopped — meeting=${this.meetingUuid} session=${sessionInfo.sessionId}`);
        this.onDisconnect(0);
      } else if (op === rtms.SESSION_EVENT_PAUSE) {
        console.log(`RTMS session paused — meeting=${this.meetingUuid} session=${sessionInfo.sessionId}`);
      } else if (op === rtms.SESSION_EVENT_RESUME) {
        console.log(`RTMS session resumed — meeting=${this.meetingUuid} session=${sessionInfo.sessionId}`);
      }
    });

    this.sdk.onMediaConnectionInterrupted((timestamp: number) => {
      console.warn(`RTMS media connection interrupted — meeting=${this.meetingUuid} timestamp=${timestamp}`);
    });

    // 2. Configure media parameters
    const paramsOk = this.sdk.setVideoParams({
      codec: rtms.VideoCodec.PNG,
      resolution: rtms.VideoResolution.HD,
      dataOpt: rtms.VideoDataOption.VIDEO_SINGLE_ACTIVE_STREAM,
      fps: this.config.FRAME_SAMPLE_RATE,
    });
    if (!paramsOk) {
      console.error(`setVideoParams failed — meeting=${this.meetingUuid}`);
      this.onError(-1);
      return;
    }

    // 3. Join — SDK reads ZM_RTMS_CLIENT / ZM_RTMS_SECRET from env for signature generation.
    const joinOk = this.sdk.join({
      meeting_uuid: this.meetingUuid,
      rtms_stream_id: this.rtmsStreamId,
      server_urls: this.serverUrls,
    });
    if (!joinOk) {
      console.error(`join() returned false — meeting=${this.meetingUuid}`);
      this.onError(-1);
      return;
    }

    console.log(`RTMSClient starting — meeting=${this.meetingUuid}`);
  }

  close(): void {
    if (this.sdk) {
      this.sdk.leave();
      this.sdk = null;
      console.log(`RTMSClient closed — meeting=${this.meetingUuid}`);
    }
  }
}
