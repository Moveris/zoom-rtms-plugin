import express from "express";
import type { Config } from "./config.js";
import type { SessionOrchestrator } from "./orchestrator.js";
import type { ResultStore } from "./results.js";
import { devRouter } from "./routes/dev.js";
import { healthRouter } from "./routes/health.js";
import { oauthRouter } from "./routes/oauth.js";
import { resultsRouter } from "./routes/results.js";
import { webhookRouter } from "./routes/webhook.js";

export function createApp(
  config: Config,
  orchestrator: SessionOrchestrator,
  resultStore: ResultStore,
): express.Express {
  const app = express();

  // JSON parsing for non-webhook routes.
  // The webhook route uses rtms.createWebhookHandler() which handles its own body parsing.
  app.use((req, _res, next) => {
    if (req.path === "/zoom/webhook") {
      next();
    } else {
      express.json()(req, _res, next);
    }
  });

  app.use(webhookRouter(orchestrator));
  app.use(oauthRouter(config));
  app.use(devRouter(config));
  app.use(resultsRouter(resultStore));
  app.use(healthRouter(orchestrator));

  return app;
}
