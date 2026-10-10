import { useState, type Dispatch } from "react";
import { api } from "../lib/api";
import { awaiting, resolved, type Action, type State } from "../lib/store";
import { OrderTicket } from "./OrderTicket";
import { PlanTicket } from "./PlanTicket";
import { Empty, Section } from "./ui";

/** Everything that needs the trader's decision, and what happened to what they already decided.
 *  This is the only place a click can send an order, and each click echoes the hash on the card. */
export function ApprovalDock({ state, dispatch }: { state: State; dispatch: Dispatch<Action> }) {
  const [sending, setSending] = useState<Record<string, boolean>>({});
  const waiting = awaiting(state);
  const done = resolved(state);
  const count = waiting.orders.length + waiting.plans.length;
  const accountKind = state.account?.account_kind;

  const busy = (id: string, on: boolean) => setSending((s) => ({ ...s, [id]: on }));
  const note = (id: string, tone: "warn" | "error" | "info", text: string) =>
    dispatch({ type: "note", id, note: { tone, text } });

  async function approveOrder(id: string, hash: string, acknowledgment?: string) {
    busy(id, true);
    const r = await api.approveOrder(id, hash, acknowledgment);
    busy(id, false);
    if (r.ok) return dispatch({ type: "result", id, result: r.data });
    const c = r.conflict;
    note(id, c?.code === "REQUOTE_REQUIRED" ? "warn" : c?.code === "AWAITING_CO_CAPTAIN" ? "info" : "error", r.message);
    // the replacement card arrives on the live feed; flag it so the trader sees why it changed
    if (c?.code === "REQUOTE_REQUIRED" && c.pending) note(c.pending.id, "warn", r.message);
  }

  async function approvePlan(id: string, hash: string) {
    busy(id, true);
    const r = await api.approvePlan(id, hash);
    busy(id, false);
    if (r.ok) return dispatch({ type: "report", report: r.data });
    const c = r.conflict;
    note(id, c?.code === "REQUOTE_REQUIRED" ? "warn" : c?.code === "AWAITING_CO_CAPTAIN" ? "info" : "error", r.message);
    if (c?.code === "REQUOTE_REQUIRED" && c.plan) note(c.plan.id, "warn", r.message);
  }

  async function decline(kind: "order" | "plan", id: string) {
    busy(id, true);
    const r = kind === "order" ? await api.declineOrder(id) : await api.declinePlan(id);
    busy(id, false);
    if (!r.ok) note(id, "error", r.message);
  }

  return (
    <Section
      id="approvals"
      title="Needs your approval"
      aside={count ? `${count} waiting` : "nothing waiting"}
    >
      <div className="space-y-4" aria-live="polite">
        {count === 0 && done.orders.length + done.plans.length === 0 && (
          <Empty title="Nothing is waiting for you.">
            When you ask for an order, it appears here as a ticket. Nothing is sent until you approve that exact ticket.
          </Empty>
        )}

        {waiting.plans.map((p) => (
          <PlanTicket
            key={p.id}
            plan={p}
            note={state.notes[p.id]}
            sending={!!sending[p.id]}
            onApprove={() => approvePlan(p.id, p.plan_hash)}
            onDecline={() => decline("plan", p.id)}
            onDismiss={() => dispatch({ type: "hide", id: p.id })}
            accountKind={accountKind}
          />
        ))}
        {waiting.orders.map((o) => (
          <OrderTicket
            key={o.id}
            order={o}
            note={state.notes[o.id]}
            result={state.results[o.id]}
            sending={!!sending[o.id]}
            onApprove={(acknowledgment) => approveOrder(o.id, o.order_hash, acknowledgment)}
            onDecline={() => decline("order", o.id)}
            onDismiss={() => dispatch({ type: "hide", id: o.id })}
            accountKind={accountKind}
          />
        ))}

        {done.plans.map((p) => (
          <PlanTicket
            key={p.id}
            plan={p}
            report={state.reports[p.id]}
            note={state.notes[p.id]}
            sending={false}
            onApprove={() => {}}
            onDecline={() => {}}
            onDismiss={() => dispatch({ type: "hide", id: p.id })}
            accountKind={accountKind}
          />
        ))}
        {done.orders.map((o) => (
          <OrderTicket
            key={o.id}
            order={o}
            note={state.notes[o.id]}
            result={state.results[o.id]}
            sending={false}
            onApprove={() => {}}
            onDecline={() => {}}
            onDismiss={() => dispatch({ type: "hide", id: o.id })}
            accountKind={accountKind}
          />
        ))}
      </div>
    </Section>
  );
}
