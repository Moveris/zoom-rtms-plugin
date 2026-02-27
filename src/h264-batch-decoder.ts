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

/** If no data arrives for this long during accumulation, abort. */
const INACTIVITY_TIMEOUT_MS = 5_000;

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
  /** 10 consecutive PNG buffers, 640x480. */
  frames: Buffer[];
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
   * Returns 10 consecutive PNG frames (640x480) from the middle of the decoded video.
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

      if (rawFrames.length < 10) {
        throw new Error(
          `Only decoded ${rawFrames.length} frames from ${h264Data.length} bytes ` +
          `(${this.chunkCount} chunks over ${(elapsedMs / 1000).toFixed(1)}s). ` +
          `NAL types: {${nalSummary}}. Need at least 10 frames.`,
        );
      }

      // Pick 10 CONSECUTIVE frames from the middle of the batch.
      // Moveris requires temporal continuity — frames MUST be consecutive.
      const startIdx = Math.floor((rawFrames.length - 10) / 2);
      const selectedRaw = rawFrames.slice(startIdx, startIdx + 10);

      console.log(`[H264Diag:${this.label}] Selected consecutive frames ${startIdx}-${startIdx + 9} out of ${rawFrames.length}`);

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

      return { frames: pngFrames };
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
 * Run ffmpeg on an H264 file and return all decoded raw RGB frames.
 */
function runFfmpeg(inputPath: string, label = "unknown"): Promise<Buffer[]> {
  return new Promise((resolve, reject) => {
    const ffmpeg = spawn("ffmpeg", [
      "-hide_banner",
      "-loglevel", "info",
      "-f", "h264",
      "-i", inputPath,
      "-vf", `scale=${TARGET_WIDTH}:${TARGET_HEIGHT}`,
      "-f", "rawvideo",
      "-pix_fmt", "rgb24",
      "pipe:1",
    ], {
      stdio: ["ignore", "pipe", "pipe"],
    });

    const outputChunks: Buffer[] = [];
    let stderrOutput = "";

    ffmpeg.stdout.on("data", (chunk: Buffer) => {
      outputChunks.push(chunk);
    });

    ffmpeg.stderr.on("data", (data: Buffer) => {
      stderrOutput += data.toString();
    });

    ffmpeg.on("close", (code) => {
      // Always log ffmpeg output for diagnostics (upgraded from warn to log)
      if (stderrOutput.trim()) {
        console.log(`[H264Diag:${label}] ffmpeg stderr (exit=${code}):\n${stderrOutput.trim()}`);
      }

      // Concatenate all output and split into frames
      const allOutput = Buffer.concat(outputChunks);
      const frames: Buffer[] = [];
      let offset = 0;

      while (offset + RAW_FRAME_SIZE <= allOutput.length) {
        frames.push(Buffer.from(allOutput.subarray(offset, offset + RAW_FRAME_SIZE)));
        offset += RAW_FRAME_SIZE;
      }

      const remainderBytes = allOutput.length - offset;
      console.log(
        `[H264Diag:${label}] ffmpeg output: ${allOutput.length} bytes → ${frames.length} frames ` +
        `(${RAW_FRAME_SIZE} bytes/frame, remainder=${remainderBytes} bytes)`,
      );

      if (frames.length === 0 && code !== 0) {
        reject(new Error(`ffmpeg exited with code ${code}, decoded 0 frames. Output size: ${allOutput.length} bytes`));
        return;
      }

      resolve(frames);
    });

    ffmpeg.on("error", (err) => {
      reject(new Error(`ffmpeg spawn error: ${err.message}`));
    });
  });
}
