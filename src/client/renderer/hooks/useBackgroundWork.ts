import { useCallback, useEffect, useState } from "react";
import {
  getBackgroundWorkStatus,
  startMemoryBackgroundWork,
  startTadaBackgroundWork,
} from "../api/client";

type Feature = "memory" | "tada";

export function useBackgroundWork(connected: boolean, feature: Feature) {
  const [status, setStatus] = useState<BackgroundWorkFeatureStatus | null>(null);
  const [starting, setStarting] = useState(false);
  const [startFailed, setStartFailed] = useState(false);

  const load = useCallback(async () => {
    if (!connected) return;
    const res = await getBackgroundWorkStatus();
    setStatus(res[feature]);
  }, [connected, feature]);

  useEffect(() => {
    load().catch(() => {});
    if (!connected) return;
    const timer = window.setInterval(() => {
      load().catch(() => {});
    }, 60000);
    return () => window.clearInterval(timer);
  }, [connected, load]);

  const startNow = useCallback(async () => {
    setStarting(true);
    setStartFailed(false);
    try {
      if (feature === "tada") await startTadaBackgroundWork();
      else await startMemoryBackgroundWork();
      await load();
    } catch {
      setStartFailed(true);
      await load().catch(() => {});
    } finally {
      setStarting(false);
    }
  }, [feature, load]);

  return { status, starting, startFailed, load, startNow };
}
