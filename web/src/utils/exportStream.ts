import { baseUrl } from "@/api/baseUrl";
import { FrigateConfig } from "@/types/frigateConfig";
import { RecordStream } from "@/types/record";

export function cameraHasSecondaryRecording(
  config: FrigateConfig | undefined,
  camera: string,
): boolean {
  return Boolean(config?.cameras[camera]?.record?.secondary?.enabled);
}

export function anyCameraHasSecondaryRecording(
  config: FrigateConfig | undefined,
  cameras: string[],
): boolean {
  return cameras.some((camera) => cameraHasSecondaryRecording(config, camera));
}

/**
 * Keep a secondary choice only for cameras that actually record one.
 * A missing config still honors an explicit secondary choice so a click
 * is not silently downgraded before config loads.
 */
export function exportStreamForCamera(
  config: FrigateConfig | undefined,
  camera: string,
  selected: RecordStream,
): RecordStream {
  if (selected !== "secondary") {
    return "primary";
  }

  if (!config) {
    return "secondary";
  }

  return cameraHasSecondaryRecording(config, camera) ? "secondary" : "primary";
}

export function recordingExportVodUrl(
  camera: string,
  after: number,
  before: number,
  stream: RecordStream,
): string {
  const bounds = `start/${after}/end/${before}/index.m3u8`;

  if (stream === "secondary") {
    return `${baseUrl}vod/${camera}/stream/secondary/${bounds}`;
  }

  return `${baseUrl}vod/${camera}/${bounds}`;
}
