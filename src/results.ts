import type { ParticipantResult, SessionStatus } from "./types.js";

export interface ResultStore {
  createSession(meetingUuid: string): void;
  setSessionState(meetingUuid: string, state: SessionStatus["state"]): void;
  setResult(meetingUuid: string, participantId: string, result: ParticipantResult): void;
  getSession(meetingUuid: string): SessionStatus | null;
}

export class InMemoryResultStore implements ResultStore {
  private sessions = new Map<string, SessionStatus>();

  createSession(meetingUuid: string): void {
    if (this.sessions.has(meetingUuid)) return;
    this.sessions.set(meetingUuid, {
      meetingUuid,
      state: "pending",
      participants: {},
      startedAt: new Date(),
    });
  }

  setSessionState(meetingUuid: string, state: SessionStatus["state"]): void {
    const session = this.sessions.get(meetingUuid);
    if (!session) return;
    session.state = state;
    if (state === "complete" || state === "error") {
      session.completedAt = new Date();
    }
  }

  setResult(meetingUuid: string, participantId: string, result: ParticipantResult): void {
    const session = this.sessions.get(meetingUuid);
    if (!session) return;
    session.participants[participantId] = result;
  }

  getSession(meetingUuid: string): SessionStatus | null {
    return this.sessions.get(meetingUuid) ?? null;
  }
}
