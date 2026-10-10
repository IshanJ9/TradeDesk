// How an instrument is named on screen. Mirrors Instrument.label on the backend (app/schemas.py).
import type { Instrument } from "./types";

export const isOption = (i: Pick<Instrument, "option_type">): boolean => i.option_type != null;

/** "Infosys Ltd" / "INFY", or "NIFTY 24,500 CE (13 Oct 2026)" for an option contract. */
export function instrumentLabel(i: Instrument): string {
  if (!isOption(i) || i.strike == null || !i.expiry) return i.symbol;
  const strike = i.strike % 100 === 0 ? (i.strike / 100).toLocaleString("en-IN") : (i.strike / 100).toFixed(2);
  const [y, m, d] = i.expiry.split("-").map(Number);
  const when = new Date(Date.UTC(y!, m! - 1, d!)).toLocaleDateString("en-GB", { day: "2-digit", month: "short", year: "numeric", timeZone: "UTC" });
  return `${i.underlying} ${strike} ${i.option_type} (${when})`;
}

/** "1 lot (75 units)" for an option, or the plain quantity for shares. */
export function sizeText(quantity: number, i: Instrument): string {
  if (!isOption(i) || !i.lot_size) return String(quantity);
  const lots = Math.floor(quantity / i.lot_size);
  return `${lots} lot${lots === 1 ? "" : "s"} (${quantity} units)`;
}
