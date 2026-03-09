import { spawn } from "child_process";
import { writeFile, unlink } from "fs/promises";
import { tmpdir } from "os";
import { join } from "path";
import { randomBytes } from "crypto";
import sharp from "sharp";

const TARGET_WIDTH = 640;
const TARGET_HEIGHT = 480;
const RAW_FRAME_SIZE = TARGET_WIDTH * TARGET_HEIGHT * 3;

/** Duration of H264 data to accumulate before batch decoding. */
const ACCUMULATE_DURATION_MS = 4_000;

/** Number of consecutive frames to select for Moveris liveness analysis. */
const FRAME_COUNT = 10;

/** If no data arrives for this long during accumulation, abort. */
const INACTIVITY_TIMEOUT_MS = 5_000;

/** Max time to allow ffmpeg to run before killing it (ms). */
const FFMPEG_TIMEOUT_MS = 30_000;

/**
 * Max frames to output from ffmpeg. At 30fps over 4s we'd get ~120 frames
 * (118MB of raw RGB), which OOM-kills the 512MB Fly VM. Capping at 40 frames
 * (~37MB) keeps peak memory well under the limit. We only need 10 consecutive.
 */
const FFMPEG_MAX_FRAMES = 40;

/** How often to log a delivery-rate summary during accumulation. */
const DIAG_LOG_INTERVAL_MS = 1_000;

/** H.264 NAL unit type names for diagnostics. */
const NAL_TYPE_NAMES: Record<number, string> = {
  1: "non-IDR",
  5: "IDR",
  6: "SEI",
  7: "SPS",
  8: "PPS",
  9: "AUD",
};

function nalTypeName(nalType: number): string {
  return NAL_TYPE_NAMES[nalType] ?? `unknown(${nalType})`;
}

/**
 * Parse the NAL unit type from the first bytes of an H.264 chunk.
 * Looks for Annex B start codes (0x000001 or 0x00000001) then reads the NAL type.
 */
function parseNalType(chunk: Buffer): number | null {
  if (chunk.length < 4) return null;

  let offset = 0;
  // Check for 4-byte start code (0x00000001)
  if (chunk[0] === 0 && chunk[1] === 0 && chunk[2] === 0 && chunk[3] === 1) {
    offset = 4;
  // Check for 3-byte start code (0x000001)
  } else if (chunk[0] === 0 && chunk[1] === 0 && chunk[2] === 1) {
    offset = 3;
  } else {
    // No start code — NAL byte is at offset 0 (raw NAL unit)
    offset = 0;
  }

  if (offset >= chunk.length) return null;
  return chunk[offset] & 0x1f; // NAL unit type is lower 5 bits
}

export interface BatchDecodeResult {
  /** Consecutive PNG buffers, 640x480 (FRAME_COUNT frames). */
  frames: Buffer[];
  /** Timestamp (ms since epoch) when the first H264 chunk was received. */
  firstChunkTime: number;
  /** Timestamp (ms since epoch) when the last H264 chunk was received. */
  lastChunkTime: number;
  /** Total number of frames decoded by ffmpeg (may be capped by FFMPEG_MAX_FRAMES). */
  totalDecodedFrames: number;
  /** Estimated total frames in the full H264 clip (from chunk count, uncapped). */
  estimatedTotalFrames: number;
  /** Index of the first selected frame within the decoded batch. */
  selectedStartIndex: number;
}

/**
 * Batch H264 decoder.
 *
 * Accumulates raw H264 NAL units for ~4 seconds, then decodes the entire
 * batch in a single ffmpeg invocation. This avoids the streaming buffering
 * issues that cause ffmpeg to stall when decoding H264 from a pipe.
 */
export class H264BatchDecoder {
  private chunks: Buffer[] = [];
  private totalBytes = 0;
  private firstChunkTime: number | null = null;
  private lastChunkTime: number | null = null;
  private done = false;

  // --- Diagnostic counters ---
  private chunkCount = 0;
  private nalTypeCounts = new Map<number, number>();
  private lastDiagLogTime: number | null = null;
  private label: string; // human-readable label for log lines

  constructor(label?: string) {
    this.label = label ?? "unknown";
  }

  /** Feed a raw H264 NAL unit / chunk into the accumulator. */
  feed(h264Chunk: Buffer): void {
    if (this.done) return;

    const now = Date.now();
    if (this.firstChunkTime === null) {
      this.firstChunkTime = now;
      console.log(`[H264Diag:${this.label}] First chunk received — size=${h264Chunk.length}B`);
    }
    this.lastChunkTime = now;

    this.chunks.push(h264Chunk);
    this.totalBytes += h264Chunk.length;
    this.chunkCount++;

    // Parse NAL unit type for diagnostics
    const nalType = parseNalType(h264Chunk);
    if (nalType !== null) {
      this.nalTypeCounts.set(nalType, (this.nalTypeCounts.get(nalType) ?? 0) + 1);
    }

    // Periodic delivery-rate summary (every ~1 second)
    if (this.lastDiagLogTime === null || now - this.lastDiagLogTime >= DIAG_LOG_INTERVAL_MS) {
      this.lastDiagLogTime = now;
      const elapsedSec = (now - this.firstChunkTime) / 1000;
      const chunksPerSec = elapsedSec > 0 ? (this.chunkCount / elapsedSec).toFixed(1) : "0";
      const kbPerSec = elapsedSec > 0 ? ((this.totalBytes / 1024) / elapsedSec).toFixed(1) : "0";
      console.log(
        `[H264Diag:${this.label}] ${elapsedSec.toFixed(1)}s elapsed — ` +
        `chunks=${this.chunkCount} (${chunksPerSec}/s), ` +
        `bytes=${this.totalBytes} (${kbPerSec} KB/s), ` +
        `lastChunkSize=${h264Chunk.length}B`,
      );
    }
  }

  /** Returns the elapsed accumulation time in milliseconds. */
  getElapsedMs(): number {
    if (this.firstChunkTime === null) return 0;
    return Date.now() - this.firstChunkTime;
  }

  /** Returns total bytes accumulated so far. */
  getTotalBytes(): number {
    return this.totalBytes;
  }

  /** Returns true if enough data has been accumulated (~4 seconds). */
  isReady(): boolean {
    if (this.done) return false;
    if (this.firstChunkTime === null) return false;
    return this.getElapsedMs() >= ACCUMULATE_DURATION_MS;
  }

  /** Returns true if no data has arrived for too long during accumulation. */
  isTimedOut(): boolean {
    if (this.firstChunkTime === null || this.lastChunkTime === null) return false;
    return Date.now() - this.lastChunkTime >= INACTIVITY_TIMEOUT_MS;
  }

  /**
   * Decode all accumulated H264 data in one shot.
   * Returns FRAME_COUNT consecutive PNG frames (640x480) from the middle of the decoded video.
   */
  async decode(): Promise<BatchDecodeResult> {
    this.done = true;

    if (this.chunks.length === 0) {
      throw new Error("No H264 data accumulated");
    }

    // Log batch completion diagnostics
    const elapsedMs = this.firstChunkTime ? Date.now() - this.firstChunkTime : 0;
    const nalSummary = Array.from(this.nalTypeCounts.entries())
      .map(([t, c]) => `${nalTypeName(t)}=${c}`)
      .join(", ");
    console.log(
      `[H264Diag:${this.label}] Batch complete — ` +
      `chunks=${this.chunkCount}, bytes=${this.totalBytes}, ` +
      `duration=${(elapsedMs / 1000).toFixed(1)}s, ` +
      `NAL types: {${nalSummary}}`,
    );

    // Concatenate all chunks into one buffer
    const h264Data = Buffer.concat(this.chunks);
    this.chunks = []; // free memory

    // Write to temp file
    const tmpPath = join(tmpdir(), `rtms-batch-${randomBytes(8).toString("hex")}.h264`);
    await writeFile(tmpPath, h264Data);

    console.log(`[H264Diag:${this.label}] Wrote ${h264Data.length} bytes to ${tmpPath}, starting ffmpeg decode`);

    try {
      // Run ffmpeg one-shot on the file
      const rawFrames = await runFfmpeg(tmpPath, this.label);
      console.log(
        `[H264Diag:${this.label}] FFmpeg decoded ${rawFrames.length} raw frames ` +
        `from ${h264Data.length} bytes (${(rawFrames.length / Math.max(elapsedMs / 1000, 0.1)).toFixed(1)} effective fps)`,
      );

      if (rawFrames.length < FRAME_COUNT) {
        throw new Error(
          `Only decoded ${rawFrames.length} frames from ${h264Data.length} bytes ` +
          `(${this.chunkCount} chunks over ${(elapsedMs / 1000).toFixed(1)}s). ` +
          `NAL types: {${nalSummary}}. Need at least ${FRAME_COUNT} frames.`,
        );
      }

      // Pick FRAME_COUNT CONSECUTIVE frames from the middle of the batch.
      // Moveris requires temporal continuity — frames MUST be consecutive.
      const startIdx = Math.floor((rawFrames.length - FRAME_COUNT) / 2);
      const selectedRaw = rawFrames.slice(startIdx, startIdx + FRAME_COUNT);

      console.log(`[H264Diag:${this.label}] Selected consecutive frames ${startIdx}-${startIdx + FRAME_COUNT - 1} out of ${rawFrames.length}`);

      // Convert each raw RGB frame to 640x480 PNG
      const pngFrames = await Promise.all(
        selectedRaw.map((rawRgb) =>
          sharp(rawRgb, {
            raw: { width: TARGET_WIDTH, height: TARGET_HEIGHT, channels: 3 },
          })
            .png({ compressionLevel: 1 })
            .toBuffer(),
        ),
      );

      return {
        frames: pngFrames,
        firstChunkTime: this.firstChunkTime!,
        lastChunkTime: this.lastChunkTime!,
        totalDecodedFrames: rawFrames.length,
        estimatedTotalFrames: this.chunkCount,
        selectedStartIndex: startIdx,
      };
    } finally {
      // Clean up temp file
      await unlink(tmpPath).catch(() => {});
    }
  }

  /** Mark as done without decoding (e.g., on error/cancel). */
  cancel(): void {
    this.done = true;
    this.chunks = [];
  }
}

/**
 * Run ffmpeg on an H264 file and return decoded raw RGB frames.
 * Limits output to FFMPEG_MAX_FRAMES to avoid OOM on constrained VMs.
 * Streams frames incrementally instead of buffering all output at once.
 */
function runFfmpeg(inputPath: string, label = "unknown"): Promise<Buffer[]> {
  return new Promise((resolve, reject) => {
    let settled = false;

    const ffmpeg = spawn("ffmpeg", [
      "-hide_banner",
      "-loglevel", "info",
      "-f", "h264",
      "-i", inputPath,
      "-vf", `scale=${TARGET_WIDTH}:${TARGET_HEIGHT}:force_original_aspect_ratio=decrease,pad=${TARGET_WIDTH}:${TARGET_HEIGHT}:(ow-iw)/2:(oh-ih)/2:black`,
      "-frames:v", String(FFMPEG_MAX_FRAMES),
      "-f", "rawvideo",
      "-pix_fmt", "rgb24",
      "pipe:1",
    ], {
      stdio: ["ignore", "pipe", "pipe"],
    });

    // Kill ffmpeg if it doesn't finish in time
    const timer = setTimeout(() => {
      if (!settled) {
        settled = true;
        console.error(
          `[H264Diag:${label}] ffmpeg timeout after ${FFMPEG_TIMEOUT_MS / 1000}s — killing process (pid=${ffmpeg.pid})`,
        );
        ffmpeg.kill("SIGKILL");
        reject(new Error(`ffmpeg timed out after ${FFMPEG_TIMEOUT_MS / 1000}s`));
      }
    }, FFMPEG_TIMEOUT_MS);

    // Stream-process frames: extract complete frames as they arrive
    // instead of buffering all output into one giant buffer.
    const frames: Buffer[] = [];
    let pending = Buffer.alloc(0);
    let totalOutputBytes = 0;
    let stderrOutput = "";

    ffmpeg.stdout.on("data", (chunk: Buffer) => {
      totalOutputBytes += chunk.length;
      pending = Buffer.concat([pending, chunk]);
      while (pending.length >= RAW_FRAME_SIZE) {
        frames.push(Buffer.from(pending.subarray(0, RAW_FRAME_SIZE)));
        pending = pending.subarray(RAW_FRAME_SIZE);
      }
    });

    ffmpeg.stderr.on("data", (data: Buffer) => {
      stderrOutput += data.toString();
    });

    ffmpeg.on("close", (code) => {
      clearTimeout(timer);
      if (settled) return; // already rejected by timeout
      settled = true;

      // Always log ffmpeg output for diagnostics
      if (stderrOutput.trim()) {
        console.log(`[H264Diag:${label}] ffmpeg stderr (exit=${code}):\n${stderrOutput.trim()}`);
      }

      console.log(
        `[H264Diag:${label}] ffmpeg output: ${totalOutputBytes} bytes → ${frames.length} frames ` +
        `(${RAW_FRAME_SIZE} bytes/frame, remainder=${pending.length} bytes)`,
      );

      if (frames.length === 0 && code !== 0) {
        reject(new Error(`ffmpeg exited with code ${code}, decoded 0 frames. Output size: ${totalOutputBytes} bytes`));
        return;
      }

      resolve(frames);
    });

    ffmpeg.on("error", (err) => {
      clearTimeout(timer);
      if (settled) return;
      settled = true;
      reject(new Error(`ffmpeg spawn error: ${err.message}`));
    });
  });
}
