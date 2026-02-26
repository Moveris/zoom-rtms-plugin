import { Router } from "express";
import rtms from "@zoom/rtms";
import type { SessionOrchestrator } from "../orchestrator.js";

export function webhookRouter(orchestrator: SessionOrchestrator): Router {
  const router = Router();

  // Use the SDK's built-in webhook handler for URL validation + signature verification.
  // The SDK reads ZOOM_WEBHOOK_SECRET_TOKEN from env for HMAC validation.
  const handler = rtms.createWebhookHandler(
    (payload: Record<string, any>) => {
      const event = payload.event as string;

      if (event === "meeting.rtms_started") {
        const meetingUuid = payload.payload?.meeting_uuid as string;
        const rtmsStreamId = payload.payload?.rtms_stream_id as string;
        const serverUrls = payload.payload?.server_urls as string;

        if (meetingUuid && rtmsStreamId && serverUrls) {
          try {
            orchestrator.startSession(meetingUuid, rtmsStreamId, serverUrls);
          } catch (err) {
            console.error(`Failed to start RTMS session: ${err}`);
          }
        } else {
          console.error(
            `Missing RTMS fields: meeting_uuid=${meetingUuid}, stream_id=${rtmsStreamId}, server_urls=${!!serverUrls}`,
          );
        }
      }

      if (event === "meeting.rtms_stopped") {
        const meetingUuid = payload.payload?.meeting_uuid as string;
        const stopReason = payload.payload?.stop_reason as number | undefined;
        if (meetingUuid) {
          console.log(`RTMS stopped — meeting=${meetingUuid} stop_reason=${stopReason ?? "unknown"}`);
          orchestrator.stopSession(meetingUuid);
        }
      }
    },
    "/zoom/webhook",
  );

  router.post("/zoom/webhook", (req, res) => {
    handler(req, res);
  });

  return router;
}
