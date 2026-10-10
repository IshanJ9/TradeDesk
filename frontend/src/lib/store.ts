// UI state, built only from what the server says. A pure reducer: easy to test, no hidden state.
import type {
  Account,
  AuditEvent,
  DisciplineSummary,
  ExecutionResult,
  Order,
  PendingOrder,
  Plan,
  PlanReport,
  Rule,
  TraceEvent,
  WsEvent,
} from "./types";

export interface Note {
  tone: "warn" | "error" | "info";
  text: string;
}

export interface Toast {
  id: string;
  kind: "rule" | "error" | "info";
  message: string;
  cardId?: string; // the approval card this toast is about, if any
}

export interface State {
  conn: "connecting" | "live" | "offline";
  loaded: boolean;
  account: Account | null;
  pending: Record<string, PendingOrder>;
  plans: Record<string, Plan>;
  orders: Order[]; // newest first
  rules: Rule[]; // newest first
  reports: Record<string, PlanReport>;
  audit: AuditEvent[]; // newest first
  ticks: Record<string, { ltp: number; seq: number }>;
  results: Record<string, ExecutionResult>; // what happened after a click, by card id
  notes: Record<string, Note>; // banners on a card: "price moved", "expired", ...
  hidden: Record<string, true>; // resolved cards the trader dismissed
  toasts: Toast[];
  trace: TraceEvent[]; // newest first: the assistant's steps, for the activity panel
  external: Order[]; // newest first: orders placed outside this app (021's own app)
  discipline: DisciplineSummary | null; // today vs the trader's own limits
}

export type Action =
  | { type: "conn"; status: State["conn"] }
  | { type: "event"; event: WsEvent }
  | { type: "result"; id: string; result: ExecutionResult }
  | { type: "report"; report: PlanReport }
  | { type: "note"; id: string; note: Note | null }
  | { type: "hide"; id: string }
  | { type: "toast"; toast: Omit<Toast, "id"> }
  | { type: "dismissToast"; id: string };

export const AUDIT_LIMIT = 150;
export const TRACE_LIMIT = 200;

export const initialState: State = {
  conn: "connecting",
  loaded: false,
  account: null,
  pending: {},
  plans: {},
  orders: [],
  rules: [],
  reports: {},
  audit: [],
  ticks: {},
  results: {},
  notes: {},
  hidden: {},
  toasts: [],
  trace: [],
  external: [],
  discipline: null,
};

let toastCounter = 0;
const toastId = () => `t${++toastCounter}`;

function upsert<T>(list: T[], item: T, same: (a: T) => boolean): T[] {
  return list.some(same) ? list.map((x) => (same(x) ? item : x)) : [item, ...list];
}

function byId<T extends { id: string }>(items: T[]): Record<string, T> {
  return Object.fromEntries(items.map((i) => [i.id, i]));
}

function applyEvent(s: State, e: WsEvent): State {
  switch (e.type) {
    case "snapshot": {
      // keep cards that already resolved on screen; take everything live from the snapshot
      const keptOrders = Object.fromEntries(Object.entries(s.pending).filter(([, p]) => !isLive(p)));
      const keptPlans = Object.fromEntries(Object.entries(s.plans).filter(([, p]) => !isLivePlan(p)));
      return {
        ...s,
        loaded: true,
        account: e.account,
        pending: { ...keptOrders, ...byId(e.pending.orders) },
        plans: { ...keptPlans, ...byId(e.pending.plans) },
        orders: e.orders,
        rules: e.rules,
      };
    }
    case "tick":
      return { ...s, ticks: { ...s.ticks, [e.tick.instrument_key]: { ltp: e.tick.ltp, seq: e.tick.seq } } };
    case "account_update":
      return { ...s, account: e.account };
    case "lock_update":
      return s.account ? { ...s, account: { ...s.account, locks: e.locks } } : s;
    case "order_update":
      return { ...s, orders: upsert(s.orders, e.order, (o) => o.order_id === e.order.order_id) };
    case "pending_created":
    case "pending_updated":
      return { ...s, pending: { ...s.pending, [e.pending.id]: e.pending } };
    case "plan_created":
    case "plan_updated":
      return { ...s, plans: { ...s.plans, [e.plan.id]: e.plan } };
    case "plan_report_update":
      return { ...s, reports: { ...s.reports, [e.report.plan_id]: e.report } };
    case "rule_update":
      return { ...s, rules: upsert(s.rules, e.rule, (r) => r.id === e.rule.id) };
    case "rule_fired":
      return {
        ...s,
        rules: upsert(s.rules, e.rule, (r) => r.id === e.rule.id),
        toasts: [...s.toasts, { id: toastId(), kind: "rule", message: e.message, cardId: e.pending?.id }],
      };
    case "audit_event":
      return { ...s, audit: [e.event, ...s.audit].slice(0, AUDIT_LIMIT) };
    case "chaos_status":
      return s;
    case "trace":
      return { ...s, trace: [e, ...s.trace].slice(0, TRACE_LIMIT) };
    case "external_order":
      return { ...s, external: upsert(s.external, e.order, (o) => o.order_id === e.order.order_id) };
    case "discipline_update":
      return { ...s, discipline: e.summary };
    case "cocaptain_update":
      return s; // the Co-Captain tab refreshes itself when lib/ws.ts passes this on (lib/cocaptain.ts)
  }
}

export function reducer(s: State, a: Action): State {
  switch (a.type) {
    case "conn":
      return { ...s, conn: a.status };
    case "event":
      return applyEvent(s, a.event);
    case "result":
      return { ...s, results: { ...s.results, [a.id]: a.result } };
    case "report":
      // The HTTP answer to "approve" can arrive after the live feed has already reported progress.
      // Never let that older snapshot replace newer data; it only fills in a report we don't have.
      return s.reports[a.report.plan_id] ? s : { ...s, reports: { ...s.reports, [a.report.plan_id]: a.report } };
    case "note": {
      const notes = { ...s.notes };
      if (a.note) notes[a.id] = a.note;
      else delete notes[a.id];
      return { ...s, notes };
    }
    case "hide":
      return { ...s, hidden: { ...s.hidden, [a.id]: true } };
    case "toast":
      return { ...s, toasts: [...s.toasts, { ...a.toast, id: toastId() }] };
    case "dismissToast":
      return { ...s, toasts: s.toasts.filter((t) => t.id !== a.id) };
  }
}

// ---- selectors --------------------------------------------------------------------------- //

const newestFirst = <T extends { created_at: string }>(a: T, b: T) => Date.parse(b.created_at) - Date.parse(a.created_at);
const oldestFirst = <T extends { created_at: string }>(a: T, b: T) => Date.parse(a.created_at) - Date.parse(b.created_at);

// A plan's steps are orders internally. They belong inside their plan's ticket, never on their own.
const standalone = (p: PendingOrder) => !p.plan_id;
/** Still waiting on someone: the trader's click, or (past their own limit) the Co-Captain's as well. */
export const isLivePlan = (p: Plan) => p.state === "PENDING" || p.state === "AWAITING_CO_APPROVAL";
export const isLive = (p: PendingOrder) => p.state === "PENDING" || p.state === "AWAITING_CO_APPROVAL";

/** Cards waiting for the trader's decision, oldest first. */
export function awaiting(s: State): { orders: PendingOrder[]; plans: Plan[] } {
  return {
    orders: Object.values(s.pending).filter((p) => isLive(p) && standalone(p)).sort(oldestFirst),
    plans: Object.values(s.plans).filter(isLivePlan).sort(oldestFirst),
  };
}

/** Cards that already have an outcome the trader should be able to read, newest first. */
export function resolved(s: State, limit = 6): { orders: PendingOrder[]; plans: Plan[] } {
  const shown = (id: string) => !s.hidden[id];
  return {
    orders: Object.values(s.pending).filter((p) => !isLive(p) && standalone(p) && shown(p.id)).sort(newestFirst).slice(0, limit),
    plans: Object.values(s.plans).filter((p) => !isLivePlan(p) && shown(p.id)).sort(newestFirst).slice(0, limit),
  };
}
