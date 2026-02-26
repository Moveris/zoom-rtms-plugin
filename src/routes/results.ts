import { Router } from "express";
import type { ResultStore } from "../results.js";

export function resultsRouter(resultStore: ResultStore): Router {
  const router = Router();

  router.get("/results/:meetingUuid", (req, res) => {
    const { meetingUuid } = req.params;
    const status = resultStore.getSession(meetingUuid);
    if (!status) {
      res.status(404).json({ detail: "Session not found" });
      return;
    }
    res.json(status);
  });

  return router;
}
