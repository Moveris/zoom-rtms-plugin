import { Router } from "express";
import type { Config } from "../config.js";

// In-memory token store (production should use a database).
let zoomToken: Record<string, unknown> | null = null;

export function getZoomToken(): Record<string, unknown> | null {
  return zoomToken;
}

export function oauthRouter(config: Config): Router {
  const router = Router();

  router.get("/oauth/callback", async (req, res) => {
    const code = (req.query.code as string) ?? "";
    if (!code) {
      res.status(200).send(
        "<h2>Moveris Zoom RTMS Plugin</h2><p>App installed successfully.</p>",
      );
      return;
    }

    // Build redirect_uri, respecting X-Forwarded-Proto behind reverse proxy
    let redirectUri = `${req.protocol}://${req.get("host")}${req.path}`;
    if (req.headers["x-forwarded-proto"] === "https") {
      redirectUri = redirectUri.replace("http://", "https://");
    }

    const creds = Buffer.from(
      `${config.ZOOM_CLIENT_ID}:${config.ZOOM_CLIENT_SECRET}`,
    ).toString("base64");

    try {
      const resp = await fetch(
        `https://zoom.us/oauth/token?${new URLSearchParams({
          grant_type: "authorization_code",
          code,
          redirect_uri: redirectUri,
        })}`,
        {
          method: "POST",
          headers: { Authorization: `Basic ${creds}` },
        },
      );

      if (resp.ok) {
        zoomToken = (await resp.json()) as Record<string, unknown>;
        console.log("Zoom OAuth token obtained successfully");
      } else {
        console.warn(`Token exchange failed: ${resp.status} ${await resp.text()}`);
      }
    } catch (err) {
      console.error(`OAuth error: ${err}`);
    }

    res.status(200).send(
      "<h2>Moveris Zoom RTMS Plugin</h2><p>App authorized successfully. You can close this tab.</p>",
    );
  });

  return router;
}
