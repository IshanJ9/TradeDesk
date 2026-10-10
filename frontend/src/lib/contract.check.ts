// Compile-time checks on the generated contract. Run: npm run typecheck
// If the backend contract drifts in a way the UI would trip over, this file stops compiling.
import type { components, paths } from "./types.gen";

type S = components["schemas"];

/** Every message that can arrive on /ws, taken from the OpenAPI doc route. */
export type WsEvent =
  paths["/api/ws-events"]["get"]["responses"]["200"]["content"]["application/json"][number];

/** The `default` branch fails to compile if a new event type is added without being handled here. */
export function describe(e: WsEvent): string {
  switch (e.type) {
    case "snapshot":
      return `snapshot with ${e.account.holdings.length} holdings`;
    case "tick":
      return `${e.tick.instrument_key} ${e.tick.ltp}`;
    case "account_update":
      return `account ${e.account.funds.available_cash}`;
    case "order_update":
      return `order ${e.order.order_id} ${e.order.status}`;
    case "pending_created":
    case "pending_updated":
      return `pending ${e.pending.id} ${e.pending.state}`;
    case "plan_created":
    case "plan_updated":
      return `plan ${e.plan.id} ${e.plan.state}: ${e.plan.title}`;
    case "plan_report_update":
      // show e.report.summary (written by the server) and one row per step from e.report.legs
      return `plan ${e.report.plan_id} ${e.report.state}: ${e.report.legs.map((l) => `${l.label} ${l.status}`).join(", ")}`;
    case "rule_update":
      return `rule ${e.rule.id} ${e.rule.status}`;
    case "rule_fired":
      // a standing instruction triggered: show e.message, and the approval card if there is one
      return e.pending ? `${e.message} (card ${e.pending.id})` : e.message;
    case "lock_update":
      return `anchor ${e.locks.anchor_active}`;
    case "audit_event":
      return e.event.summary;
    case "chaos_status":
      return `network_down ${e.status.network_down}`;
    case "trace":
      return `${e.run_id} ${e.node} ${e.kind} ${e.status} ${e.ms ?? ""}`;
    case "external_order":
      return `placed outside this app: ${e.order.order_id}`;
    case "discipline_update":
      return `orders today ${e.summary.orders_today}, risk ${e.summary.risk_score ?? "-"}`;
    default: {
      const unhandled: never = e;
      return unhandled;
    }
  }
}

/** Fields the server always sends must not be optional in the generated types. */
declare const pending: S["PendingOrder"];
export const approvalHash: string = pending.order_hash; // computed field
export const pendingState: "PENDING" | "AWAITING_CO_APPROVAL" | "APPROVED" | "SENT" | "REQUOTE_REQUIRED" | "EXPIRED" | "VOID" | "REJECTED" =
  pending.state; // defaulted field, still required in responses

declare const holding: S["Holding"];
export const pnlPct: number = holding.pnl_pct; // computed server-side, never by the LLM

/** Chat cards narrow by their `type`. */
export function cardKind(card: S["ChatReply"]["cards"][number]): string {
  switch (card.type) {
    case "pending_order":
      return card.pending.id;
    case "plan":
      return card.plan.id;
    case "rule":
      return card.rule.id;
    case "ambiguity":
      return card.candidates.map((c) => c.symbol).join("/");
    case "notice":
      return card.message;
  }
}

/** What happened after the trader clicked Approve. UNKNOWN must never be shown as success. */
export function describeExecution(r: S["ExecutionResult"]): string {
  switch (r.outcome) {
    case "SENT":
      return `sent: ${r.order?.status ?? "confirmed"}`;
    case "REJECTED":
      return `rejected: ${r.order?.rejection_reason ?? r.message}`;
    case "UNKNOWN":
      return `unconfirmed, not re-sent: ${r.message}`;
  }
}

/** Why an approval did not go through; all of these mean nothing was sent. */
export function describeConflict(c: S["ApprovalConflict"]): string {
  switch (c.code) {
    case "REQUOTE_REQUIRED":
      return c.pending ? `price moved; new card ${c.pending.id}` : c.message;
    case "HASH_MISMATCH":
    case "EXPIRED":
    case "NOT_PENDING":
    case "BLOCKED":
    case "ACK_REQUIRED":
    case "AWAITING_CO_CAPTAIN":
      return c.message;
  }
}

/** The cost card needs every charge line, including the delivery-sell DP charge. */
declare const charges: S["Charges"];
export const costLines: number[] = [
  charges.brokerage, charges.stt, charges.exchange_txn, charges.sebi_fee,
  charges.stamp_duty, charges.gst, charges.clearing, charges.ipft, charges.dp_charge,
  charges.total, charges.break_even_price,
];

/** Creating a rule from the UI. `price_rupees` XOR `percent`; an ALERT carries no order fields. */
export const exampleRule: S["CreateRuleRequest"] = {
  kind: "TRIGGER_ORDER",
  instrument: "TCS",
  comparator: "BELOW",
  price_rupees: 3800,
  side: "BUY",
  quantity: 5,
};

/** A plan: steps sized by exactly one of quantity / amount / fraction of holding / proceeds of an earlier sale. */
export const examplePlan: S["ProposePlanRequest"] = {
  legs: [
    { instrument: "infosys", side: "SELL", fraction_of_holding: 0.5 },
    { instrument: "itc", side: "BUY", proceeds_of_leg: 0 },
  ],
};

/** The plan card shows the estimate and the caps that the approval hash covers. */
declare const planLeg: S["PlanLeg"];
export const stepCaps: [number | null, number | null] = [planLeg.max_quantity, planLeg.max_spend];

/** Why approving a plan did not run it: all of these mean nothing was sent. */
export function describePlanConflict(c: S["ApprovalConflict"]): string {
  return c.code === "REQUOTE_REQUIRED" && c.plan ? `prices moved; new plan ${c.plan.id}` : c.message;
}
