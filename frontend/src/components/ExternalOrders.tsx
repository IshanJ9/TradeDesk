// Owner: voice-live. Read-only view of activity detected by polling.
import { clock, rupees } from "../lib/format";
import { instrumentLabel, sizeText } from "../lib/instrument";
import type { State } from "../lib/store";
import { useExternalActivity } from "../lib/externalActivity";
import { Chip, Empty } from "./ui";

export function ExternalOrders({ state }: { state: State }) {
  const { orders, loading, error, attributionPending } = useExternalActivity(state.external, state.conn);
  return (
    <div>
    <p role="status" className="mb-2 text-xs text-muted">
      {error ? "Saved activity is unavailable. Displayed activity may be stale; retrying shortly."
        : loading ? "Loading saved activity…" : "Today's saved external activity. Updates arrive by polling."}
      {attributionPending && " Some orders are awaiting source confirmation while an execution is unresolved."}
    </p>
    {!orders.length && !loading && !error && <Empty title="No external orders recorded for today.">Orders placed in 021&rsquo;s app appear after polling; completed orders are also restored from saved history.</Empty>}
    <ul aria-label="Orders detected outside TradeDesk">
      {orders.map((o) => (
        <li key={o.order_id} className="border-b border-line py-2 text-[13px] last:border-b-0">
          <div className="flex flex-wrap items-center gap-2"><span className="num min-w-0 break-all text-ink">
            {o.side === "BUY" ? "Buy" : "Sell"} {sizeText(o.quantity, o.instrument)} &times; {instrumentLabel(o.instrument)}
          </span><Chip tone={o.status === "UNKNOWN" || o.status === "REJECTED" ? "warn" : "plain"}>
            {o.status === "UNKNOWN" ? "Unknown — not confirmed" : o.status === "PARTIAL" ? "Partially filled" : o.status.charAt(0) + o.status.slice(1).toLowerCase()}
          </Chip></div>
          <p className="mt-1 text-xs text-muted">{o.instrument.exchange} · {o.product} · {o.order_type === "MARKET" ? "Market" : o.limit_price != null ? `Limit ${rupees(o.limit_price)}` : "Price not reported"}
            {o.trigger_price != null && <> · Trigger {rupees(o.trigger_price)}</>}
          </p>
          <p className="mt-1 text-xs text-muted">{o.filled_quantity} of {o.quantity} filled
            {o.avg_fill_price != null && <> · Average fill {rupees(o.avg_fill_price)}</>}
          </p>
          <p className="mt-1 break-all text-xs text-muted">Order {o.order_id} · Placed <time dateTime={o.created_at} title={o.created_at}>{clock(o.created_at)}</time> · Updated <time dateTime={o.updated_at} title={o.updated_at}>{clock(o.updated_at)}</time></p>
          <p className="mt-2 text-xs text-muted">Placed in 021's app, not by TradeDesk. It counts toward your daily activity.</p>
        </li>
      ))}
    </ul>
    </div>
  );
}
