// Plain-English wording for order cards and their outcomes. Numbers come from the server;
// this only chooses words.
import { rupees, sideWord } from "./format";
import type { Charges, ExecutionResult, PendingOrder, PlanLeg, PlanLegResult } from "./types";

export const productWord = (p: string) => (p === "CNC" ? "Delivery" : p === "MIS" ? "Intraday" : p);

export function validityWord(v: string, minutes?: number | null): string {
  if (v === "DAY") return "Valid for the day";
  if (v === "IOC") return "Fill now or cancel";
  return `Valid for ${minutes ?? "?"} min`;
}

export const REASONS: Record<string, string> = {
  INSUFFICIENT_FUNDS: "not enough funds",
  PRICE_BAND: "the price is outside today's allowed range",
  INVALID_PRICE: "the price isn't valid for this stock",
  INVALID_QUANTITY: "the quantity isn't valid",
  MARKET_CLOSED: "the market is closed",
  SUSPENDED: "the stock is suspended",
  KYC_DORMANT: "the account needs attention (KYC or dormant)",
  RISK_CHECK: "a risk check blocked it",
  OI_LIMIT: "an open-interest limit applies",
  QUANTITY_LIMIT: "the quantity is above the limit",
  VALUE_LIMIT: "the order value is above the limit",
  SEGMENT_NOT_ALLOWED: "only equity orders are supported",
  ANCHOR_ACTIVE: "Anchor is on",
  CO_APPROVAL_REQUIRED: "your Co-Captain's approval is needed",
  OTHER: "the broker refused it",
};

export function priceLine(o: Pick<PendingOrder, "side" | "limit_price" | "protection_price">): string {
  if (o.limit_price != null) {
    return o.side === "SELL" ? `Limit: at least ${rupees(o.limit_price)}` : `Limit: up to ${rupees(o.limit_price)}`;
  }
  if (o.protection_price != null) {
    return o.side === "SELL"
      ? `Market, protected: won't fill below ${rupees(o.protection_price)}`
      : `Market, protected: won't fill above ${rupees(o.protection_price)}`;
  }
  return "Market";
}

export function actionTitle(o: PendingOrder): string {
  const sym = o.instrument.symbol;
  if (o.action === "CANCEL") return `Cancel order ${o.target_order_id}`;
  if (o.action === "MODIFY") return `Change order ${o.target_order_id}`;
  return `${sideWord(o.side)} ${o.quantity} × ${sym}`;
}

/** The non-zero cost lines, in the order 021 lists them. */
export function chargeLines(c: Charges): [string, number][] {
  const all: [string, number][] = [
    ["Brokerage", c.brokerage],
    ["STT", c.stt],
    ["Exchange charges", c.exchange_txn],
    ["SEBI fee", c.sebi_fee],
    ["Stamp duty", c.stamp_duty],
    ["Investor protection fund", c.ipft],
    ["Depository (DP) charge", c.dp_charge],
    ["Clearing", c.clearing],
    ["GST", c.gst],
  ];
  return all.filter(([, v]) => v > 0);
}

export interface Outcome {
  tone: "info" | "warn" | "error";
  title: string;
  body: string;
}

/** What happened after the trader clicked Approve, in words. UNKNOWN is never shown as success. */
export function outcomeOf(r: ExecutionResult): Outcome {
  const o = r.order;
  if (r.outcome === "UNKNOWN") {
    return { tone: "warn", title: "Outcome not confirmed", body: r.message };
  }
  if (r.outcome === "REJECTED") {
    const why = o?.rejection_reason ? REASONS[o.rejection_reason] ?? o.rejection_reason : "the broker refused it";
    return { tone: "error", title: "Not placed", body: `The broker rejected this order: ${why}. Nothing was bought or sold.` };
  }
  if (!o) return { tone: "info", title: "Sent", body: "The broker accepted it." };
  const avg = o.avg_fill_price ? ` at an average of ${rupees(o.avg_fill_price)}` : "";
  switch (o.status) {
    case "FILLED":
      return { tone: "info", title: "Filled", body: `${o.filled_quantity} of ${o.quantity} filled${avg}.` };
    case "PARTIAL":
      return { tone: "info", title: "Partly filled", body: `${o.filled_quantity} of ${o.quantity} filled${avg}. The rest is still open.` };
    case "CANCELLED":
      return { tone: "info", title: "Cancelled", body: o.filled_quantity ? `${o.filled_quantity} of ${o.quantity} filled before it ended.` : "Nothing matched, so the broker cancelled it." };
    default:
      return { tone: "info", title: "Sent, waiting", body: "The broker has it. It fills when the price reaches your limit." };
  }
}

export const STATE_NOTE: Record<string, string> = {
  REQUOTE_REQUIRED: "The price moved, so this card was replaced by an updated one. Nothing was sent.",
  EXPIRED: "This card expired before it was approved. Nothing was sent.",
  VOID: "This card was cancelled. Nothing was sent.",
  REJECTED: "You declined this. Nothing was sent.",
};

export function legStatusWord(r: PlanLegResult): string {
  switch (r.status) {
    case "FILLED": return `Filled ${r.filled_quantity}/${r.requested_quantity}`;
    case "PARTIAL": return `Partly filled ${r.filled_quantity}/${r.requested_quantity}`;
    case "OPEN": return "Sent, waiting";
    case "REJECTED": return "Rejected";
    case "CANCELLED": return "Cancelled";
    case "UNKNOWN": return "Not confirmed";
    case "SKIPPED": return "Not sent";
    default: return "Waiting";
  }
}

export function legLine(leg: PlanLeg): { title: string; detail: string } {
  const o = leg.order;
  const funded = leg.quantity_basis === "FROM_PROCEEDS";
  const qty = funded ? `about ${o.quantity}` : `${o.quantity}`;
  const title = `${sideWord(o.side)} ${qty} × ${o.instrument.symbol}`;
  const detail = funded
    ? `${priceLine(o)}. Paid for with step ${(leg.proceeds_from_leg ?? 0) + 1}'s money, never more than ${leg.max_quantity} shares or ${rupees(leg.max_spend ?? 0)}.`
    : `${priceLine(o)}. About ${rupees(o.est_total)} ${o.side === "SELL" ? "after" : "with"} charges.`;
  return { title, detail };
}
