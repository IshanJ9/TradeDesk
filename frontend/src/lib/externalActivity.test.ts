import { afterEach, expect, it, vi } from "vitest";
import { loadExternalActivity, watchExternalActivity, type Activity } from "./externalActivity";

const saved: Activity = { day: "2026-10-09", orders: [], attribution_pending: false };
afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals(); });

it("loads persisted activity on initial subscription without requiring an event", async () => {
  vi.useFakeTimers();
  const receive = vi.fn();
  const load = vi.fn().mockResolvedValue(saved);
  const stop = watchExternalActivity(receive, vi.fn(), load);
  await vi.advanceTimersByTimeAsync(0);
  expect(receive).toHaveBeenCalledWith(saved);
  expect(load).toHaveBeenCalledTimes(1);
  await vi.advanceTimersByTimeAsync(5000);
  expect(load).toHaveBeenCalledTimes(2);
  stop();
});

it("discards a late request after reconnect or unmount and aborts it", async () => {
  let resolve!: (data: Activity) => void;
  const load = vi.fn(() => new Promise<Activity>((done) => { resolve = done; }));
  const receive = vi.fn();
  const stop = watchExternalActivity(receive, vi.fn(), load);
  stop();
  resolve(saved);
  await Promise.resolve();
  expect(receive).not.toHaveBeenCalled();
});

it("retries errors and accepts an authoritative empty list after correction", async () => {
  vi.useFakeTimers();
  const load = vi.fn().mockRejectedValueOnce(new Error("offline")).mockResolvedValue(saved);
  const receive = vi.fn(), failed = vi.fn();
  const stop = watchExternalActivity(receive, failed, load);
  await vi.advanceTimersByTimeAsync(0);
  expect(failed).toHaveBeenCalledTimes(1);
  await vi.advanceTimersByTimeAsync(5000);
  expect(receive).toHaveBeenCalledWith(saved);
  stop();
});

it("does not overlap slow requests", async () => {
  vi.useFakeTimers();
  const load = vi.fn(() => new Promise<Activity>(() => {}));
  const stop = watchExternalActivity(vi.fn(), vi.fn(), load);
  await vi.advanceTimersByTimeAsync(9000);
  expect(load).toHaveBeenCalledTimes(1);
  stop();
});

it("fetches only the read-only history route and hides raw failures", async () => {
  const fetch = vi.fn().mockResolvedValue(new Response("private upstream error", { status: 500 }));
  vi.stubGlobal("fetch", fetch);
  await expect(loadExternalActivity(new AbortController().signal)).rejects.toThrow("Saved activity is unavailable");
  expect(fetch.mock.calls[0]![0]).toBe("/api/activity/external");
});
