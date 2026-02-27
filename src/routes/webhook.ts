import { Router } from "express";
import rtms from "@zoom/rtms";
import type { Config } from "../config.js";
import type { SessionOrchestrator } from "../orchestrator.js";

export function webhookRouter(
  config: Config,
  orchestrator: SessionOrchestrator,
): Router {
  const router = Router();

  // Use the SDK's built-in webhook handler for URL validation + signature verification.
  // The SDK reads ZOOM_WEBHOOK_SECRET_TOKEN from env for HMAC validation.
  const handler = rtms.createWebhookHandler(
    (payload: Record<string, any>) => {
      const event = payload.event as string;

      if (event === "meeting.rtms_started") {
        const rtmsPayload = payload.payload as Record<string, any> | undefined;
        const meetingUuid = rtmsPayload?.meeting_uuid as string;

        if (meetingUuid && rtmsPayload) {
          try {
            // Check if the sidebar registered a pending session with a user-provided key
            let apiKey: string | undefined;
            let excludeUserId: string | undefined;
            if (orchestrator.hasPendingSession(meetingUuid)) {
              const pending = orchestrator.consumePendingSession(meetingUuid);
              apiKey = pending?.apiKey;
              excludeUserId = pending?.excludeUserId;
              console.log(`Using sidebar-provided API key for meeting=${meetingUuid}`);
            } else if (!config.AUTO_START_RTMS) {
              // Sidebar-only mode: no pending session means no scan requested
              console.log(`No pending session and AUTO_START_RTMS=false — ignoring meeting=${meetingUuid}`);
              return;
            }
            // Pass the raw webhook payload directly — the SDK's join() expects this format.
            orchestrator.startSession(meetingUuid, rtmsPayload, apiKey, excludeUserId);
          } catch (err) {
            console.error(`Failed to start RTMS session: ${err}`);
          }
        } else {
          console.error(`Missing RTMS payload or meeting_uuid in webhook`);
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
