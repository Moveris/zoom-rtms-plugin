import { Router } from "express";
import type { SessionOrchestrator } from "../orchestrator.js";
import { getZoomToken } from "./oauth.js";

export function healthRouter(orchestrator: SessionOrchestrator): Router {
  const router = Router();

  router.get("/health", (_req, res) => {
    res.json({
      status: "ok",
      version: "0.1.0",
      active_sessions: orchestrator.activeSessionCount,
      zoom_token: getZoomToken() ? "present" : "missing",
    });
  });

  return router;
}
