// Owner: voice-live. Orders placed outside this app (in 021's own app), from ExternalOrderEvent. Placeholder.
import { clock } from "../lib/format";
import type { State } from "../lib/store";
import { Empty } from "./ui";

export function ExternalOrders({ state }: { state: State }) {
  if (!state.external.length)
    return <Empty title="Nothing placed outside this app today.">Orders you place in 021&rsquo;s own app show up here too.</Empty>;
  return (
    <ul>
      {state.external.map((o) => (
        <li key={o.order_id} className="border-b border-line py-2 text-[13px] last:border-b-0">
          <span className="num text-ink">
            {o.side === "BUY" ? "Buy" : "Sell"} {o.quantity} &times; {o.instrument.symbol}
          </span>{" "}
          <span className="text-xs text-muted">
            {o.status.toLowerCase()} &middot; {clock(o.created_at)}
          </span>
        </li>
      ))}
    </ul>
  );
}
