import { useEffect, useRef, useState } from "react";

import { ExportMode } from "@/types/filter";
import { RecordStream } from "@/types/record";

/**
 * Stream chosen for the next export.
 *
 * Resets when the export form opens. The History player passes the stream
 * currently on screen so the form starts on what the user is watching.
 * Callers without a player default to the continuous (secondary) stream.
 */
export function useExportStream(
  mode: ExportMode,
  preferredStream?: RecordStream,
): [RecordStream, (stream: RecordStream) => void] {
  const [exportStream, setExportStream] = useState<RecordStream>(
    preferredStream ?? "secondary",
  );
  const previousModeRef = useRef(mode);

  useEffect(() => {
    if (mode === "select" && previousModeRef.current === "none") {
      setExportStream(preferredStream ?? "secondary");
    }

    previousModeRef.current = mode;
  }, [mode, preferredStream]);

  return [exportStream, setExportStream];
}
