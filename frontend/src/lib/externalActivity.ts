import { useEffect, useState } from "react";
import type { components } from "./types.gen";
import type { Order } from "./types";

export type Activity = components["schemas"]["ExternalActivity"];

export async function loadExternalActivity(signal: AbortSignal): Promise<Activity> {
  const response = await fetch("/api/activity/external", { signal });
  if (!response.ok) throw new Error("Saved activity is unavailable. Retrying shortly.");
  return await response.json() as Activity;
}

/** One request at a time. Cancelling a subscription prevents old results replacing new ones. */
export function watchExternalActivity(
  receive: (data: Activity) => void, failed: () => void,
  load: typeof loadExternalActivity = loadExternalActivity,
): () => void {
  let disposed = false;
  let timer: ReturnType<typeof setTimeout> | undefined;
  let deadline: ReturnType<typeof setTimeout> | undefined;
  let controller: AbortController;
  const refresh = async () => {
    controller = new AbortController();
    deadline = setTimeout(() => controller.abort(), 10_000);
    try {
      const data = await load(controller.signal);
      if (!disposed) receive(data);
    } catch {
      if (!disposed) failed();
    } finally {
      clearTimeout(deadline);
      if (!disposed) timer = setTimeout(() => void refresh(), 5000);
    }
  };
  void refresh();
  return () => { disposed = true; clearTimeout(timer); clearTimeout(deadline); controller?.abort(); };
}

export function useExternalActivity(live: Order[], connection: string) {
  const [saved, setSaved] = useState<Activity | null>(null);
  const [error, setError] = useState(false);
  useEffect(() => watchExternalActivity((data) => { setSaved(data); setError(false); }, () => setError(true)), [live, connection]);
  // Once loaded, saved history is authoritative: a later correction may remove a
  // formerly external order still present in the session reducer.
  return { orders: saved?.orders ?? live, loading: saved === null && !error, error,
    attributionPending: saved?.attribution_pending ?? false };
}
