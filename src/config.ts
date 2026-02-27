import crypto from "node:crypto";
import "dotenv/config";
import { z } from "zod";

const configSchema = z.object({
  ZOOM_CLIENT_ID: z.string().min(1),
  ZOOM_CLIENT_SECRET: z.string().min(1),
  ZOOM_WEBHOOK_SECRET_TOKEN: z.string().min(1),
  MOVERIS_API_KEY: z.string().optional(),
  FRAME_SAMPLE_RATE: z.coerce.number().int().positive().default(5),
  LIVENESS_THRESHOLD: z.coerce.number().int().default(65),
  MAX_CONCURRENT_SESSIONS: z.coerce.number().int().positive().default(50),
  AUTO_START_RTMS: z
    .enum(["true", "false"])
    .default("true")
    .transform((v) => v === "true"),
  JWT_SECRET: z.string().default(() => crypto.randomBytes(32).toString("hex")),
  LOG_LEVEL: z.string().default("info"),
  PORT: z.coerce.number().int().default(8080),
});

export type Config = z.infer<typeof configSchema>;

let _config: Config | null = null;

export function getConfig(): Config {
  if (!_config) {
    _config = configSchema.parse(process.env);
  }
  return _config;
}
