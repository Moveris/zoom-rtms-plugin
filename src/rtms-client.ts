import rtms from "@zoom/rtms";
import type { Metadata, SessionInfo } from "@zoom/rtms";

/**
 * Fires for each raw H264 chunk received from a participant.
 * The chunk has NOT been decoded — it's a raw H264 NAL unit.
 */
export type H264ChunkCallback = (
  h264Data: Buffer,
  userId: number,
  userName: string,
  timestampMs: number,
) => void;

/** Fires when the RTMS SDK successfully joins a meeting. */
export type JoinCallback = () => void;

/** Fires as soon as video data arrives for a user — before decoding. */
export type ParticipantSeenCallback = (userId: number, userName: string) => void;

export type SessionErrorCallback = (reason: number) => void;

export class RTMSClient {
  private sdk: InstanceType<typeof rtms.Client> | null = null;
  private seenUsers = new Set<number>();
  private meetingUuid: string;
  private payload: Record<string, any>;
  private onH264Chunk: H264ChunkCallback;
  private onJoin: JoinCallback;
  private onParticipantSeen: ParticipantSeenCallback;
  private onError: SessionErrorCallback;
  private onDisconnect: SessionErrorCallback;

  constructor(
    meetingUuid: string,
    payload: Record<string, any>,
    onH264Chunk: H264ChunkCallback,
    onJoin: JoinCallback,
    onParticipantSeen: ParticipantSeenCallback,
    onError: SessionErrorCallback,
    onDisconnect: SessionErrorCallback,
  ) {
    this.meetingUuid = meetingUuid;
    this.payload = payload;
    this.onH264Chunk = onH264Chunk;
    this.onJoin = onJoin;
    this.onParticipantSeen = onParticipantSeen;
    this.onError = onError;
    this.onDisconnect = onDisconnect;
  }

  start(): void {
    this.sdk = new rtms.Client();

    // H264 at 30fps HD
    this.sdk.setVideoParams({
      contentType: rtms.VideoContentType.RAW_VIDEO,
      codec: rtms.VideoCodec.H264,
      resolution: rtms.VideoResolution.HD,
      dataOpt: rtms.VideoDataOption.VIDEO_SINGLE_ACTIVE_STREAM,
      fps: 30,
    });

    this.sdk.onVideoData((data: Buffer, size: number, timestamp: number, metadata: Metadata) => {
      const h264Chunk = data.subarray(0, size);
      const userId = metadata.userId;

      // Notify immediately on first sight of a user (before decoding)
      if (!this.seenUsers.has(userId) && userId !== 0) {
        this.seenUsers.add(userId);
        this.onParticipantSeen(userId, metadata.userName);
      }

      // Pass raw H264 chunk directly — no decoding at this layer
      this.onH264Chunk(h264Chunk, userId, metadata.userName, timestamp);
    });

    this.sdk.onJoinConfirm((reason: number) => {
      if (reason === 0) {
        console.log(`RTMS joined — meeting=${this.meetingUuid}`);
        this.onJoin();
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

    const joinOk = this.sdk.join(this.payload as any);
    if (!joinOk) {
      console.error(`join() returned false — meeting=${this.meetingUuid}`);
      this.onError(-1);
      return;
    }

    console.log(`RTMSClient starting — meeting=${this.meetingUuid}`);
  }

  close(): void {
    this.seenUsers.clear();

    if (this.sdk) {
      this.sdk.leave();
      this.sdk = null;
      console.log(`RTMSClient closed — meeting=${this.meetingUuid}`);
    }
  }
}
