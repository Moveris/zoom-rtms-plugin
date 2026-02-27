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

  /** Feed a raw H264 NAL unit / chunk into the accumulator. */
  feed(h264Chunk: Buffer): void {
    if (this.done) return;

    const now = Date.now();
    if (this.firstChunkTime === null) {
      this.firstChunkTime = now;
    }
    this.lastChunkTime = now;

    this.chunks.push(h264Chunk);
    this.totalBytes += h264Chunk.length;
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

    // Concatenate all chunks into one buffer
    const h264Data = Buffer.concat(this.chunks);
    this.chunks = []; // free memory

    // Write to temp file
    const tmpPath = join(tmpdir(), `rtms-batch-${randomBytes(8).toString("hex")}.h264`);
    await writeFile(tmpPath, h264Data);

    console.log(`H264BatchDecoder: wrote ${h264Data.length} bytes to ${tmpPath}, starting decode`);

    try {
      // Run ffmpeg one-shot on the file
      const rawFrames = await runFfmpeg(tmpPath);
      console.log(`H264BatchDecoder: decoded ${rawFrames.length} raw frames`);

      if (rawFrames.length < 10) {
        throw new Error(`Only decoded ${rawFrames.length} frames (need at least 10)`);
      }

      // Pick 10 CONSECUTIVE frames from the middle of the batch.
      // Moveris requires temporal continuity — frames MUST be consecutive.
      const startIdx = Math.floor((rawFrames.length - 10) / 2);
      const selectedRaw = rawFrames.slice(startIdx, startIdx + 10);

      console.log(`H264BatchDecoder: selected consecutive frames ${startIdx}-${startIdx + 9} out of ${rawFrames.length}`);

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
function runFfmpeg(inputPath: string): Promise<Buffer[]> {
  return new Promise((resolve, reject) => {
    const ffmpeg = spawn("ffmpeg", [
      "-hide_banner",
      "-loglevel", "warning",
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
      if (stderrOutput.trim()) {
        console.warn(`ffmpeg batch decode stderr: ${stderrOutput.trim()}`);
      }

      // Concatenate all output and split into frames
      const allOutput = Buffer.concat(outputChunks);
      const frames: Buffer[] = [];
      let offset = 0;

      while (offset + RAW_FRAME_SIZE <= allOutput.length) {
        frames.push(Buffer.from(allOutput.subarray(offset, offset + RAW_FRAME_SIZE)));
        offset += RAW_FRAME_SIZE;
      }

      if (frames.length === 0 && code !== 0) {
        reject(new Error(`ffmpeg exited with code ${code}, decoded 0 frames`));
        return;
      }

      resolve(frames);
    });

    ffmpeg.on("error", (err) => {
      reject(new Error(`ffmpeg spawn error: ${err.message}`));
    });
  });
}
