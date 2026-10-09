// Display formatting only. The server computes every figure; this turns paise into text.

const MINUS = "−";

/** 1234567 -> "12,34,567" (Indian digit grouping). */
export function indianGroup(n: number): string {
  const s = String(Math.trunc(Math.abs(n)));
  if (s.length <= 3) return s;
  let head = s.slice(0, -3);
  const tail = s.slice(-3);
  const parts: string[] = [];
  while (head.length > 2) {
    parts.unshift(head.slice(-2));
    head = head.slice(0, -2);
  }
  if (head) parts.unshift(head);
  return [...parts, tail].join(",");
}

/** Integer paise -> "₹1,450.00" (negative: "−₹710.00"). */
export function rupees(paise: number, opts: { plus?: boolean } = {}): string {
  const sign = paise < 0 ? MINUS : opts.plus && paise > 0 ? "+" : "";
  const abs = Math.abs(paise);
  const whole = Math.trunc(abs / 100);
  const frac = String(abs % 100).padStart(2, "0");
  return `${sign}₹${indianGroup(whole)}.${frac}`;
}

/** -7.24 -> "−7.24%", 0.56 -> "+0.56%". */
export function pct(value: number): string {
  const sign = value < 0 ? MINUS : value > 0 ? "+" : "";
  return `${sign}${Math.abs(value).toFixed(2)}%`;
}

export function sideWord(side: string | null | undefined): string {
  return side === "BUY" ? "Buy" : side === "SELL" ? "Sell" : "";
}

export function shortHash(hash: string): string {
  return `${hash.slice(0, 4)} ${hash.slice(4, 8)} ${hash.slice(8, 12)} ${hash.slice(12, 16)}`;
}

/** Whole seconds left until an ISO timestamp (never negative). */
export function secondsLeft(expiresAt: string, nowMs: number): number {
  return Math.max(0, Math.ceil((Date.parse(expiresAt) - nowMs) / 1000));
}

export function clock(iso: string): string {
  const d = new Date(iso);
  return d.toLocaleTimeString("en-IN", { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });
}
