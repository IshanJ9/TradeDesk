// How an instrument is named on screen. Mirrors Instrument.label on the backend (app/schemas.py).
import type { Instrument } from "./types";

export const isOption = (i: Pick<Instrument, "option_type">): boolean => i.option_type != null;
export const isFuture = (i: Pick<Instrument, "series">): boolean => i.series === "FUT";
/** An option or a future: counted in lots, and not a share. */
export const isDerivative = (i: Pick<Instrument, "option_type" | "series">): boolean => isOption(i) || isFuture(i);

function expiryText(expiry: string): string {
  const [y, m, d] = expiry.split("-").map(Number);
  return new Date(Date.UTC(y!, m! - 1, d!)).toLocaleDateString("en-GB", { day: "2-digit", month: "short", year: "numeric", timeZone: "UTC" });
}

/** "Infosys Ltd" / "INFY", "NIFTY 24,500 CE (13 Oct 2026)" for an option, "NIFTY FUT (27 Oct 2026)" for a future. */
export function instrumentLabel(i: Instrument): string {
  if (isFuture(i) && i.expiry) return `${i.underlying} FUT (${expiryText(i.expiry)})`;
  if (!isOption(i) || i.strike == null || !i.expiry) return i.symbol;
  const strike = i.strike % 100 === 0 ? (i.strike / 100).toLocaleString("en-IN") : (i.strike / 100).toFixed(2);
  return `${i.underlying} ${strike} ${i.option_type} (${expiryText(i.expiry)})`;
}

/** "1 lot (75 units)" for an option or future, or the plain quantity for shares. */
export function sizeText(quantity: number, i: Instrument): string {
  if (!isDerivative(i) || !i.lot_size) return String(quantity);
  const lots = Math.floor(quantity / i.lot_size);
  return `${lots} lot${lots === 1 ? "" : "s"} (${quantity} units)`;
}
