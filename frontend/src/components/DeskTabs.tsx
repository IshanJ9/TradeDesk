import { useState, type Dispatch } from "react";
import { api } from "../lib/api";
import { REASONS, productWord } from "../lib/describe";
import { clock, rupees } from "../lib/format";
import { instrumentLabel, sizeText } from "../lib/instrument";
import type { Action, State } from "../lib/store";
import type { AuditEvent, Order, Rule } from "../lib/types";
import { AssistantTrace } from "./AssistantTrace";
import { CoCaptainPanel } from "./CoCaptainPanel";
import { DisciplinePanel } from "./DisciplinePanel";
import { useCoCaptain } from "../lib/cocaptain";
import { ExternalOrders } from "./ExternalOrders";
import { Button, Chip, Empty, Section } from "./ui";

type Tab = "orders" | "rules" | "assistant" | "external" | "discipline" | "cocaptain" | "log";

function OrderRow({ o, ask }: { o: Order; ask: (text: string) => void }) {
  const open = o.status === "OPEN" || o.status === "PARTIAL";
  const tone = o.status === "FILLED" ? "gain" : o.status === "REJECTED" ? "loss" : open ? "warn" : "plain";
  const price = o.trigger_price ? `stop ${rupees(o.trigger_price)}` : o.limit_price ? rupees(o.limit_price) : "market";
  return (
    <li className="flex items-start justify-between gap-3 border-b border-line py-2.5 text-[13px] last:border-b-0">
      <div className="min-w-0">
        <div className="num font-medium text-ink">
          {o.side === "BUY" ? "Buy" : "Sell"} {sizeText(o.quantity, o.instrument)} &times; {instrumentLabel(o.instrument)} <span className="text-muted">@ {price}</span>
        </div>
        <div className="text-xs text-muted">
          <span className="num">{o.order_id}</span> &middot; {productWord(o.product)} &middot; {clock(o.created_at)}
          {o.filled_quantity > 0 && <> &middot; {o.filled_quantity} filled{o.avg_fill_price ? <> at <span className="num">{rupees(o.avg_fill_price)}</span></> : null}</>}
          {o.status === "REJECTED" && o.rejection_reason && <> &middot; {REASONS[o.rejection_reason] ?? o.rejection_reason}</>}
        </div>
      </div>
      <div className="flex shrink-0 items-center gap-2">
        <Chip tone={tone}>{o.status === "OPEN" ? "Open" : o.status.charAt(0) + o.status.slice(1).toLowerCase()}</Chip>
        {open && <Button variant="plain" className="px-2 py-1 text-xs" onClick={() => ask(`cancel order ${o.order_id}`)}>Cancel&hellip;</Button>}
      </div>
    </li>
  );
}

function RuleRow({ r, onCancel }: { r: Rule; onCancel: () => void }) {
  const tone = r.status === "ACTIVE" ? "info" : r.status === "FIRED" ? "warn" : "plain";
  return (
    <li className="border-b border-line py-2.5 text-[13px] last:border-b-0">
      <div className="flex items-center justify-between gap-3">
        <div className="flex items-center gap-2">
          <Chip tone={tone}>{r.status === "ACTIVE" ? "Active" : r.status === "FIRED" ? "Fired" : "Cancelled"}</Chip>
          <Chip>{r.kind === "ALERT" ? "Alert" : "Order rule"}</Chip>
          <span className="num text-xs text-muted">{r.id}</span>
        </div>
        {r.status === "ACTIVE" && <Button variant="plain" className="px-2 py-1 text-xs" onClick={onCancel}>Cancel rule</Button>}
      </div>
      <p className="mt-1.5 text-ink">{r.description}</p>
      <p className="num mt-0.5 text-xs text-muted">
        Trigger {rupees(r.condition.trigger_price)}
        {r.fired_at && <> &middot; fired {clock(r.fired_at)}</>}
      </p>
    </li>
  );
}

function LogRow({ e }: { e: AuditEvent }) {
  const tone = e.kind === "INJECTION_BLOCKED" || e.kind === "APPROVAL_REFUSED" || e.kind.endsWith("BLOCKED") ? "warn" : "plain";
  return (
    <li className="grid grid-cols-[4.5rem_1fr] gap-2 border-b border-line py-2 text-[13px] last:border-b-0">
      <span className="num text-xs text-muted">{clock(e.ts)}</span>
      <div className="min-w-0">
        <Chip tone={tone}>{e.kind.toLowerCase().replaceAll("_", " ")}</Chip>
        <p className="mt-0.5 break-words text-ink">{e.summary}</p>
      </div>
    </li>
  );
}

export function DeskTabs({ state, dispatch, ask }: { state: State; dispatch: Dispatch<Action>; ask: (t: string) => void }) {
  const [tab, setTab] = useState<Tab>("orders");
  const co = useCoCaptain();
  const activeRules = state.rules.filter((r) => r.status === "ACTIVE").length;

  async function cancelRule(id: string) {
    const r = await api.cancelRule(id);
    if (!r.ok) dispatch({ type: "toast", toast: { kind: "error", message: r.message } });
  }

  const tabs: [Tab, string][] = [
    ["orders", `Orders${state.orders.length ? ` (${state.orders.length})` : ""}`],
    ["rules", `Standing rules${activeRules ? ` (${activeRules})` : ""}`],
    ["assistant", "Assistant"],
    ["external", `021 app${state.external.length ? ` (${state.external.length})` : ""}`],
    ["discipline", "Discipline"],
    ["cocaptain", `Co-Captain${co.attention ? ` (${co.attention})` : ""}`],
    ["log", "Session log"],
  ];

  return (
    <Section title="Activity">
      <div role="tablist" aria-label="Activity" className="scroll-quiet mb-2 flex gap-1 overflow-x-auto border-b border-line">
        {tabs.map(([key, label]) => (
          <button
            key={key}
            role="tab"
            aria-selected={tab === key}
            onClick={(e) => { setTab(key); e.currentTarget.scrollIntoView({ block: "nearest", inline: "nearest" }); }}
            className={`-mb-px shrink-0 whitespace-nowrap border-b-2 px-3 py-2 text-[13px] font-medium ${tab === key ? "border-ink text-ink" : "border-transparent text-muted hover:text-ink"}`}
          >
            {label}
          </button>
        ))}
      </div>

      <div role="tabpanel" className="pb-6">
        {tab === "orders" &&
          (state.orders.length ? (
            <ul>{state.orders.map((o) => <OrderRow key={o.order_id} o={o} ask={ask} />)}</ul>
          ) : (
            <Empty title="No orders yet today.">Orders you approve appear here with their live status.</Empty>
          ))}

        {tab === "rules" &&
          (state.rules.length ? (
            <ul>{state.rules.map((r) => <RuleRow key={r.id} r={r} onCancel={() => cancelRule(r.id)} />)}</ul>
          ) : (
            <Empty title="No standing rules.">
              Try &ldquo;Buy 5 TCS if it falls below 3800&rdquo;. When it triggers you get a ticket to approve; nothing is sent on its own.
            </Empty>
          ))}

        {tab === "assistant" && <AssistantTrace state={state} />}
        {tab === "external" && <ExternalOrders state={state} />}
        {tab === "discipline" && <DisciplinePanel state={state} />}
        {tab === "cocaptain" && <CoCaptainPanel view={co} dispatch={dispatch} />}

        {tab === "log" && (
          <>
            <div className="mb-1 flex items-center justify-between text-xs text-muted">
              <span>Every request, approval and broker call, newest first.</span>
              <a className="underline decoration-dotted underline-offset-2 hover:text-ink" href={api.auditExportUrl} download>
                Download full log
              </a>
            </div>
            {state.audit.length ? (
              <ul>{state.audit.map((e) => <LogRow key={e.id} e={e} />)}</ul>
            ) : (
              <Empty title="Nothing logged yet.">Actions in this session appear here as they happen.</Empty>
            )}
          </>
        )}
      </div>
    </Section>
  );
}
