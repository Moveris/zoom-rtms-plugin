import path from "node:path";
import { fileURLToPath } from "node:url";
import { Router } from "express";
import express from "express";
import jwt from "jsonwebtoken";
import type { Config } from "../config.js";
import type { ApiKeyStore } from "../api-key-store.js";
import { decryptZoomContext } from "../zoom-context.js";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const SIDEBAR_DIR = path.resolve(__dirname, "../sidebar/public");

export function sidebarRouter(config: Config, apiKeyStore: ApiKeyStore): Router {
  const router = Router();

  // Serve sidebar static files
  router.use("/sidebar", express.static(SIDEBAR_DIR));

  // Auth: decrypt Zoom app context → return signed JWT
  router.post("/api/sidebar/auth", (req, res) => {
    const { context } = req.body as { context?: string };
    if (!context) {
      res.status(400).json({ error: "Missing context" });
      return;
    }

    try {
      const zoomCtx = decryptZoomContext(context, config.ZOOM_CLIENT_SECRET);
      const token = jwt.sign(
        { uid: zoomCtx.uid, mid: zoomCtx.mid },
        config.JWT_SECRET,
        { expiresIn: "4h" },
      );
      res.json({
        token,
        meetingUuid: zoomCtx.mid,
        userId: zoomCtx.uid,
        hasApiKey: apiKeyStore.has(zoomCtx.uid),
      });
    } catch (err) {
      console.error(`Zoom context decryption failed: ${err}`);
      res.status(401).json({ error: "Invalid context" });
    }
  });

  // Store API key for this Zoom account
  router.post("/api/sidebar/api-key", (req, res) => {
    const authHeader = req.headers.authorization;
    if (!authHeader?.startsWith("Bearer ")) {
      res.status(401).json({ error: "Missing authorization" });
      return;
    }

    let payload: jwt.JwtPayload;
    try {
      payload = jwt.verify(authHeader.slice(7), config.JWT_SECRET) as jwt.JwtPayload;
    } catch {
      res.status(401).json({ error: "Invalid token" });
      return;
    }

    const { apiKey } = req.body as { apiKey?: string };
    if (!apiKey) {
      res.status(400).json({ error: "Missing apiKey" });
      return;
    }

    try {
      apiKeyStore.set(payload.uid as string, apiKey);
      res.json({ ok: true });
    } catch (err) {
      res.status(400).json({ error: (err as Error).message });
    }
  });

  // Check if API key is configured for this account
  router.get("/api/sidebar/api-key/status", (req, res) => {
    const authHeader = req.headers.authorization;
    if (!authHeader?.startsWith("Bearer ")) {
      res.status(401).json({ error: "Missing authorization" });
      return;
    }

    let payload: jwt.JwtPayload;
    try {
      payload = jwt.verify(authHeader.slice(7), config.JWT_SECRET) as jwt.JwtPayload;
    } catch {
      res.status(401).json({ error: "Invalid token" });
      return;
    }

    res.json({ hasApiKey: apiKeyStore.has(payload.uid as string) });
  });

  return router;
}
