import sharp from "sharp";
import { analyzeBlur, rgbaToGrayscale, DEFAULT_BLUR_THRESHOLD } from "@moveris/shared";

const TARGET_WIDTH = 640;
const TARGET_HEIGHT = 480;

export async function resizeFrame(pngBuffer: Buffer): Promise<Buffer> {
  return sharp(pngBuffer)
    .resize(TARGET_WIDTH, TARGET_HEIGHT, { fit: "cover" })
    .png()
    .toBuffer();
}

export async function isQualityFrame(pngBuffer: Buffer): Promise<boolean> {
  const { data, info } = await sharp(pngBuffer)
    .ensureAlpha()
    .raw()
    .toBuffer({ resolveWithObject: true });

  const grayscale = rgbaToGrayscale(Array.from(data));
  const blur = analyzeBlur(grayscale, info.width, info.height, DEFAULT_BLUR_THRESHOLD);
  return !blur.isBlurry;
}
