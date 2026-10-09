import { actionTitle, chargeLines, outcomeOf, priceLine, productWord, STATE_NOTE, validityWord } from "../lib/describe";
import { rupees, secondsLeft } from "../lib/format";
import type { Note } from "../lib/store";
import type { ExecutionResult, PendingOrder } from "../lib/types";
import { Fingerprint } from "./Fingerprint";
import { Banner, Button, Chip, useNow } from "./ui";

function Countdown({ expiresAt }: { expiresAt: string }) {
  const now = useNow(1000);
  const left = secondsLeft(expiresAt, now);
  if (left === 0) return <span className="text-[var(--warn-ink)]">Expired. Ask again for a fresh card.</span>;
  const text = left >= 120 ? `${Math.floor(left / 60)} min` : `${left}s`;
  return <span className={left <= 10 ? "text-[var(--warn-ink)]" : "text-muted"}>Valid for {text}</span>;
}

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex items-baseline justify-between gap-4 py-1">
      <dt className="text-muted">{label}</dt>
      <dd className="text-right text-ink">{children}</dd>
    </div>
  );
}

interface Props {
  order: PendingOrder;
  note?: Note;
  result?: ExecutionResult;
  sending: boolean;
  onApprove: () => void;
  onDecline: () => void;
  onDismiss: () => void;
}

/** One order, on a ticket. While it is waiting the trader can approve or decline it; afterwards it
 *  keeps its place on screen with what happened. */
export function OrderTicket({ order: o, note, result, sending, onApprove, onDecline, onDismiss }: Props) {
  const waiting = o.state === "PENDING";
  const expired = waiting && secondsLeft(o.expires_at, Date.now()) === 0;
  const isCancel = o.action === "CANCEL";
  const sideTone = o.side === "SELL" ? "loss" : o.side === "BUY" ? "gain" : "plain";
  const costs = chargeLines(o.charges);
  const outcome = result ? outcomeOf(result) : null;

  return (
    <article
      id={`card-${o.id}`}
      className="ticket"
      data-tone={note?.tone === "warn" ? "warn" : undefined}
      data-resolved={!waiting}
      aria-label={actionTitle(o)}
    >
      <header className="flex items-start justify-between gap-3 px-4 pt-3.5">
        <div>
          <div className="flex items-center gap-2">
            {o.side && <Chip tone={sideTone}>{o.side}</Chip>}
            {o.action !== "PLACE" && <Chip tone="info">{o.action === "MODIFY" ? "Change" : "Cancel"}</Chip>}
            {o.rule_id && <Chip tone="info">From your rule</Chip>}
          </div>
          <h3 className="num mt-1.5 text-[20px] font-medium leading-tight text-ink">{actionTitle(o)}</h3>
          <p className="text-[13px] text-muted">
            {o.instrument.name || o.instrument.symbol} &middot; {o.instrument.exchange}
            {!isCancel && <> &middot; {productWord(o.product)}</>}
          </p>
        </div>
        {waiting && (
          <div className="shrink-0 pt-1 text-right text-xs">
            <Countdown expiresAt={o.expires_at} />
          </div>
        )}
      </header>

      {note && waiting && (
        <div className="px-4 pt-3">
          <Banner tone={note.tone} role="status">{note.text}</Banner>
        </div>
      )}

      {!isCancel && (
        <dl className="mx-4 mt-3 border-y border-line py-1.5 text-[13px]">
          <Row label="Price"><span className="num">{priceLine(o)}</span></Row>
          <Row label="Price right now"><span className="num">{rupees(o.ref_ltp)}</span></Row>
          <Row label={o.side === "SELL" ? "Estimated proceeds" : "Estimated total"}>
            <span className="num font-medium">{rupees(o.est_total)}</span>
            <span className="ml-1 text-muted">{o.side === "SELL" ? "after charges" : "with charges"}</span>
          </Row>
          <Row label="Order lasts">{validityWord(o.validity)}</Row>
        </dl>
      )}

      {!isCancel && costs.length > 0 && (
        <details className="mx-4 mt-2 text-[13px]">
          <summary className="flex items-center justify-between rounded-md py-1 text-muted hover:text-ink">
            <span>Costs &middot; <span className="num">{rupees(o.charges.total)}</span></span>
            <span className="text-xs underline decoration-dotted underline-offset-2">Show breakdown</span>
          </summary>
          <dl className="mt-1 rounded-lg bg-surface2 px-3 py-1.5">
            {costs.map(([label, v]) => (
              <Row key={label} label={label}><span className="num">{rupees(v)}</span></Row>
            ))}
            <Row label="Break-even price"><span className="num">{rupees(o.charges.break_even_price)}</span></Row>
          </dl>
        </details>
      )}

      {o.warnings.length > 0 && waiting && (
        <ul className="mx-4 mt-3 space-y-1.5">
          {o.warnings.map((w) => (
            <li key={w}><Banner tone="warn">{w}</Banner></li>
          ))}
        </ul>
      )}

      {waiting ? (
        <>
          <div className="mt-4"><div className="perf" /></div>
          <footer className="flex flex-wrap items-center justify-between gap-3 px-4 pb-3.5 pt-3">
            <Fingerprint hash={o.order_hash} />
            <div className="flex gap-2">
              <Button onClick={onDecline} disabled={sending}>Decline</Button>
              <Button variant="primary" onClick={onApprove} disabled={sending || expired}>
                {sending ? "Sending…" : isCancel ? "Approve this cancellation" : "Approve this order"}
              </Button>
            </div>
          </footer>
        </>
      ) : (
        <footer className="space-y-2 px-4 pb-3.5 pt-3">
          {outcome ? (
            <Banner tone={outcome.tone === "info" ? "info" : outcome.tone} role="status">
              <strong className="font-semibold">{outcome.title}.</strong> {outcome.body}
            </Banner>
          ) : (
            <Banner tone={o.state === "SENT" ? "info" : "warn"}>
              {STATE_NOTE[o.state] ?? "Sent to the broker."}
            </Banner>
          )}
          {note && <Banner tone={note.tone}>{note.text}</Banner>}
          <div className="flex items-center justify-between gap-3">
            <Fingerprint hash={o.order_hash} label="This card" />
            <Button variant="plain" onClick={onDismiss}>Dismiss</Button>
          </div>
        </footer>
      )}
    </article>
  );
}
