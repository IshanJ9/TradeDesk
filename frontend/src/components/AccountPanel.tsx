import { pct, rupees } from "../lib/format";
import { instrumentLabel, isDerivative, isFuture, sizeText } from "../lib/instrument";
import type { Account, Holding, Position } from "../lib/types";
import { Banner, Empty, Section, Signed } from "./ui";

type Row = Holding | Position;

function ValuedTable({ title, rows, hideDaily }: { title: string; rows: Row[]; hideDaily: boolean }) {
  return (
    <div>
      <h3 className="mb-1 text-xs font-medium uppercase tracking-[0.08em] text-muted">{title}</h3>
      <div className="overflow-hidden rounded-xl border border-line bg-paper">
        <div className="grid grid-cols-[minmax(0,1.5fr)_3rem_minmax(0,1fr)_minmax(0,1fr)_minmax(0,1.2fr)] gap-x-2 border-b border-line px-3 py-1.5 text-[11px] uppercase tracking-[0.06em] text-muted">
          <span>Stock</span>
          <span className="text-right">Qty</span>
          <span className="text-right">Avg price</span>
          <span className="text-right">Price</span>
          <span className="text-right">Profit / loss</span>
        </div>
        {rows.map((r) => (
          <div
            key={r.instrument.symbol + r.quantity}
            className="grid grid-cols-[minmax(0,1.5fr)_3rem_minmax(0,1fr)_minmax(0,1fr)_minmax(0,1.2fr)] items-center gap-x-2 border-b border-line px-3 py-2 text-[13px] last:border-b-0"
          >
            <div className="min-w-0">
              <div className="num truncate font-medium text-ink">{instrumentLabel(r.instrument)}</div>
              <div className="truncate text-xs text-muted">{isDerivative(r.instrument) ? `${isFuture(r.instrument) ? "Future" : "Option"} · ${sizeText(Math.abs(r.quantity), r.instrument)}${r.quantity < 0 ? " short" : ""}` : r.instrument.name}</div>
            </div>
            <span className="num text-right">{r.quantity}</span>
            <span className="num text-right text-muted">{rupees(r.avg_price)}</span>
            <span className="num text-right">{rupees(r.ltp)}</span>
            <div className="text-right leading-tight">
              <Signed value={r.pnl}>{rupees(r.pnl, { plus: true })}</Signed>
              <div className="num text-xs">
                <Signed value={r.pnl}>{pct(r.pnl_pct)}</Signed>
              </div>
              {!hideDaily && (
                <div className="num text-[11px] text-muted">
                  Today <Signed value={r.day_pnl}>{rupees(r.day_pnl, { plus: true })}</Signed>
                </div>
              )}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

export function AccountPanel({ account, live }: { account: Account | null; live: boolean }) {
  if (!account) {
    return (
      <Section title="Your account">
        <p className="text-muted">Loading your account&hellip;</p>
      </Section>
    );
  }
  const { funds, holdings, positions, locks } = account;
  return (
    <Section
      id="account"
      title="Your account"
      aside={
        <span className="inline-flex items-center gap-1.5">
          <span className={`h-1.5 w-1.5 rounded-full ${live ? "bg-gain" : "bg-strong"}`} aria-hidden="true" />
          {live ? "Updating live" : "Not updating"}
        </span>
      }
    >
      <div className="space-y-3">
        {locks.anchor_active && (
          <Banner tone="info" role="status">
            <strong className="font-semibold">Anchor is on.</strong> New orders are paused. You can still look around
            and cancel open orders.
            {locks.anchor_message && <> Your note to yourself: &ldquo;{locks.anchor_message}&rdquo;</>}
          </Banner>
        )}
        {locks.co_captain_locked && (
          <Banner tone="info" role="status">
            <strong className="font-semibold">Your Co-Captain has locked new orders.</strong>
            {locks.co_captain_message && <> &ldquo;{locks.co_captain_message}&rdquo;</>}
          </Banner>
        )}
        {locks.buffett_mode && (
          <Banner tone="info">Buffett Mode is on, so day-to-day movement is hidden. Overall profit and loss still shows.</Banner>
        )}

        <div className="grid grid-cols-2 gap-3">
          <div className="rounded-xl border border-line bg-paper px-3 py-2.5">
            <div className="text-xs text-muted">Available cash</div>
            <div className="num text-[17px] font-medium text-ink">{rupees(funds.available_cash)}</div>
          </div>
          <div className="rounded-xl border border-line bg-paper px-3 py-2.5">
            <div className="text-xs text-muted">Margin in use</div>
            <div className="num text-[17px] font-medium text-ink">{rupees(funds.used_margin)}</div>
          </div>
        </div>

        {holdings.length > 0 ? (
          <ValuedTable title="Holdings" rows={holdings} hideDaily={locks.buffett_mode} />
        ) : (
          <Empty title="No holdings yet.">Shares you buy for the long term show up here.</Empty>
        )}
        {positions.length > 0 && <ValuedTable title="Positions today" rows={positions} hideDaily={locks.buffett_mode} />}
        {holdings.length > 0 && <p className="text-xs text-muted">Profit and loss is measured against your average buy price.</p>}
      </div>
    </Section>
  );
}
