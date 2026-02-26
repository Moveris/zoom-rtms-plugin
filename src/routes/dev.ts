import { Router } from "express";
import type { Config } from "../config.js";
import { getZoomToken } from "./oauth.js";

export function devRouter(config: Config): Router {
  const router = Router();

  router.post("/dev/start-rtms/:meetingId", async (req, res) => {
    const token = getZoomToken();
    if (!token) {
      res.status(401).json({
        detail:
          "No Zoom OAuth token. Re-authorize the app by visiting the Zoom Marketplace app page and clicking 'Add' again.",
      });
      return;
    }

    const { meetingId } = req.params;
    const accessToken = token.access_token as string;

    try {
      const resp = await fetch(
        `https://api.zoom.us/v2/live_meetings/${meetingId}/rtms_app/status`,
        {
          method: "PATCH",
          headers: {
            Authorization: `Bearer ${accessToken}`,
            "Content-Type": "application/json",
          },
          body: JSON.stringify({
            action: "start",
            settings: { client_id: config.ZOOM_CLIENT_ID },
          }),
        },
      );

      const body = resp.headers.get("content-length") !== "0"
        ? await resp.json().catch(() => ({}))
        : {};
      console.log(`RTMS start API → ${resp.status} ${JSON.stringify(body)}`);
      res.json({ status: resp.status, response: body });
    } catch (err) {
      console.error(`RTMS start error: ${err}`);
      res.status(500).json({ detail: "Failed to start RTMS" });
    }
  });

  return router;
}
