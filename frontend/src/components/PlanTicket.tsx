import { SIMULATED } from "../lib/broker";
import { useState } from "react";
import { legLine, legStatusWord, STATE_NOTE } from "../lib/describe";
import { rupees, secondsLeft } from "../lib/format";
import { crossesOwnLimit, riskAcknowledged } from "../lib/limits";
import type { Note } from "../lib/store";
import type { Plan, PlanLegResult, PlanReport } from "../lib/types";
import { Fingerprint } from "./Fingerprint";
import { Banner, Button, Chip, useNow } from "./ui";

function Countdown({ expiresAt }: { expiresAt: string }) {
  const now = useNow(1000);
  const left = secondsLeft(expiresAt, now);
  if (left === 0) return <span className="text-[var(--warn-ink)]">Expired. Ask again for a fresh plan.</span>;
  return <span className={left <= 10 ? "text-[var(--warn-ink)]" : "text-muted"}>Valid for {left}s</span>;
}

function statusTone(r: PlanLegResult): "gain" | "loss" | "warn" | "plain" {
  switch (r.status) {
    case "FILLED": return "gain";
    case "REJECTED": return "loss";
    case "PARTIAL": case "OPEN": case "UNKNOWN": return "warn";
    default: return "plain";
  }
}

/** Where each step of an approved plan stands. Nothing is hidden: rejected and unsent steps show too. */
export function PlanProgress({ report }: { report: PlanReport }) {
  return (
    <ol className="space-y-2" aria-label="Plan progress">
      {report.legs.map((r) => (
        <li key={r.index} className="rounded-lg bg-surface2 px-3 py-2 text-[13px]">
          <div className="flex items-center justify-between gap-2">
            <span className="num font-medium text-ink">{r.index + 1}. {r.label}</span>
            <Chip tone={statusTone(r)}>{legStatusWord(r)}</Chip>
          </div>
          {r.avg_fill_price != null && r.filled_quantity > 0 && (
            <p className="num mt-0.5 text-muted">Average price {rupees(r.avg_fill_price)}</p>
          )}
          {r.status === "PARTIAL" && <p className="mt-0.5 text-muted">{r.pending_quantity} still pending.</p>}
          {r.message && <p className="mt-0.5 text-muted">{r.message}</p>}
        </li>
      ))}
    </ol>
  );
}

interface Props {
  plan: Plan;
  report?: PlanReport;
  note?: Note;
  sending: boolean;
  onApprove: () => void;
  onDecline: () => void;
  onDismiss: () => void;
  accountKind?: "mock" | "021"; // the simulated account is labelled on every ticket
}

export function PlanTicket({ plan, report, note, sending, onApprove, onDecline, onDismiss, accountKind }: Props) {
  const awaitingCo = plan.state === "AWAITING_CO_APPROVAL"; // the trader has approved; their Co-Captain has not yet
  const waiting = plan.state === "PENDING" || awaitingCo;
  const expired = waiting && secondsLeft(plan.expires_at, Date.now()) === 0;
  const running = plan.state === "APPROVED" || plan.state === "RUNNING";
  const n = plan.legs.length;
  const ownLimit = crossesOwnLimit(plan.legs.flatMap((leg) => leg.order.warnings)); // a tick before Approve (lib/limits.ts)
  const ackKey = JSON.stringify([plan.id, plan.plan_hash, plan.legs.flatMap(leg => leg.order.warnings)]);
  const [ack, setAck] = useState({ key: "", text: "" });
  const acknowledged = ack.key === ackKey && riskAcknowledged(ack.text);

  return (
    <article id={`card-${plan.id}`} className="ticket" data-tone={note?.tone === "warn" ? "warn" : undefined} data-resolved={!waiting} aria-label={plan.title}>
      <header className="flex items-start justify-between gap-3 px-4 pt-3.5">
        <div>
          <div className="flex flex-wrap items-center gap-2">
            <Chip tone="info">Plan &middot; {n} steps, approved together</Chip>
            {accountKind === "mock" && <Chip tone="info">{SIMULATED}</Chip>}
          </div>
          <h3 className="mt-1.5 font-serif text-[19px] leading-tight text-ink">{plan.title}</h3>
        </div>
        {waiting && <div className="shrink-0 pt-1 text-right text-xs"><Countdown expiresAt={plan.expires_at} /></div>}
      </header>

      {awaitingCo && (
        <div className="px-4 pt-3">
          <Banner tone="info" role="status">
            Waiting for your Co-Captain, {plan.co_captain_name ?? plan.co_captain}, to approve this same plan. Nothing has been sent.
            <ul className="mt-1 list-disc pl-4">{plan.co_reasons.map((r) => <li key={r}>{r}</li>)}</ul>
          </Banner>
        </div>
      )}
      {note && waiting && <div className="px-4 pt-3"><Banner tone={note.tone} role="status">{note.text}</Banner></div>}

      {waiting ? (
        <ol className="mx-4 mt-3 space-y-px border-y border-line py-1.5">
          {plan.legs.map((leg) => {
            const line = legLine(leg);
            return (
              <li key={leg.index} className="grid grid-cols-[1.5rem_1fr] gap-2 py-1.5 text-[13px]">
                <span className="num text-muted">{leg.index + 1}.</span>
                <div>
                  <div className="num font-medium text-ink">{line.title}</div>
                  <div className="text-muted">{line.detail}</div>
                  {leg.order.warnings.map((w) => (
                    <div key={w} className="mt-1.5"><Banner tone="warn">{w}</Banner></div>
                  ))}
                </div>
              </li>
            );
          })}
        </ol>
      ) : (
        report && <div className="mx-4 mt-3"><PlanProgress report={report} /></div>
      )}

      {waiting ? (
        <>
          <p className="mx-4 mt-2.5 text-[13px] text-muted">
            Nothing is sent until you approve. {plan.on_leg_failure === "HALT"
              ? "If a step is rejected or doesn't complete, the later steps are not sent."
              : "If a step fails, the others are still tried, except any that need its money."}
          </p>
          {ownLimit && (
            <label className="mx-4 mt-2 flex flex-col items-start gap-2 text-[13px] text-ink">
              <input aria-label="Type I UNDERSTAND to acknowledge your risk warnings" autoComplete="off" className="min-w-0 rounded border border-strong bg-surface px-2 py-1" value={ack.key === ackKey ? ack.text : ""} onChange={e => setAck({ key: ackKey, text: e.target.value })} />
              Type I UNDERSTAND. This crosses a limit you set.
            </label>
          )}
          <div className="mt-3"><div className="perf" /></div>
          <footer className="flex flex-wrap items-center justify-between gap-3 px-4 pb-3.5 pt-3">
            <Fingerprint hash={plan.plan_hash} label="Plan fingerprint" />
            <div className="flex gap-2">
              <Button onClick={onDecline} disabled={sending}>Decline</Button>
              <Button variant="primary" onClick={onApprove} disabled={sending || expired || (ownLimit && !acknowledged)}>
                {sending ? "Starting…" : `Approve all ${n} steps`}
              </Button>
            </div>
          </footer>
        </>
      ) : (
        <footer className="space-y-2 px-4 pb-3.5 pt-3">
          {report && !running && <p className="text-[13px] text-ink">{report.summary.split("\n")[0]}</p>}
          {running && <Banner tone="info" role="status">Running the steps in order&hellip;</Banner>}
          {!report && STATE_NOTE[plan.state] && <Banner tone="warn">{STATE_NOTE[plan.state]!.replace("card", "plan")}</Banner>}
          {note && <Banner tone={note.tone}>{note.text}</Banner>}
          <div className="flex items-center justify-between gap-3">
            <Fingerprint hash={plan.plan_hash} label="This plan" />
            {!running && <Button variant="plain" onClick={onDismiss}>Dismiss</Button>}
          </div>
        </footer>
      )}
    </article>
  );
}
