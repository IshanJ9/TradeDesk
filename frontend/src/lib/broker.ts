// Which broker account this desk is on (the simulated one, or the user's own 021 account) and whether it needs a
// reconnect. Shared by the header badge, the reconnect banner and the Settings dialog.
import { useSyncExternalStore } from "react";
import { brokerApi, type BrokerStatus } from "./api";

let current: BrokerStatus | null = null;
const listeners = new Set<() => void>();

function set(next: BrokerStatus | null): void {
  current = next;
  listeners.forEach((l) => l());
}

export function useBroker(): BrokerStatus | null {
  return useSyncExternalStore((l) => { listeners.add(l); return () => { listeners.delete(l); }; }, () => current);
}

export async function refreshBroker(): Promise<void> {
  const r = await brokerApi.status();
  if (r.ok) set(r.data);
}

export function forgetBroker(): void {
  set(null);
}

async function change(call: () => ReturnType<typeof brokerApi.link>): Promise<{ ok: true } | { ok: false; message: string }> {
  const r = await call();
  if (!r.ok) return { ok: false, message: r.message };
  set(r.data);
  return { ok: true };
}

export const linkBroker = (username: string, password: string) => change(() => brokerApi.link(username, password));
export const unlinkBroker = () => change(() => brokerApi.unlink());
export const reconnectBroker = () => change(() => brokerApi.reconnect());

/** For tests: what the shared state holds right now. */
export const sessionStateForTest = (): BrokerStatus | null => current;

export const SIMULATED = "Simulated account (not real money)";
