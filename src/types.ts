import type { LivenessResult } from "@moveris/shared";

export interface ParticipantResult {
  meetingUuid: string;
  participantId: string;
  result: LivenessResult | null;
  completedAt: Date;
  error?: string;
}

export interface SessionStatus {
  meetingUuid: string;
  state: "pending" | "processing" | "complete" | "error";
  participants: Record<string, ParticipantResult>;
  startedAt: Date;
  completedAt?: Date;
}
