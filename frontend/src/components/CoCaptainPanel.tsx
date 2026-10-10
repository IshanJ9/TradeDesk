// Co-Captain: a second person who must also approve an order, but only when the trader is past a limit they set.
// This panel is for both roles: the trader invites and removes their Co-Captain; the Co-Captain accepts and reviews.
import { useState, type Dispatch } from "react";
import { cocaptainApi } from "../lib/api";
import { priceLine, actionTitle } from "../lib/describe";
import { clock } from "../lib/format";
import type { CoCaptainView } from "../lib/cocaptain";
import type { Action } from "../lib/store";
import { Banner, Button, Chip, Empty } from "./ui";

export function CoCaptainPanel({ view, dispatch }: { view: CoCaptainView; dispatch: Dispatch<Action> }) {
  const { status, inbox, refresh } = view;
  const [who, setWho] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [problem, setProblem] = useState<string | null>(null);

  async function run(key: string, call: () => Promise<{ ok: boolean; message?: string }>) {
    setBusy(key);
    setProblem(null);
    const r = await call();
    setBusy(null);
    if (!r.ok && r.message) setProblem(r.message);
    await refresh();
  }

  async function review(id: string, hash: string, approve: boolean) {
    setBusy(id);
    setProblem(null);
    const r = approve ? await cocaptainApi.approve(id, hash) : await cocaptainApi.decline(id);
    setBusy(null);
    if (!r.ok) setProblem(r.message);
    else dispatch({ type: "toast", toast: { kind: "info", message: approve ? "You approved it. The order was sent." : "You declined it. Nothing was sent." } });
    await refresh();
  }

  if (!status) return <Empty title="Loading…">Checking your Co-Captain settings.</Empty>;
  const mine = status.as_trader;

  return (
    <div className="space-y-4 text-[13px]">
      <p className="text-muted">
        A Co-Captain is someone you trust who has to approve an order too, but only when you are past a limit you set
        yourself. Otherwise nothing changes. Their approval never overrides a hard stop, a lock or the broker. It is a
        person, never the assistant, and they approve by clicking in the app.
      </p>

      {problem && <Banner tone="warn" role="alert">{problem}</Banner>}

      <section aria-label="Your Co-Captain" className="rounded-xl border border-line bg-surface p-3">
        <h3 className="font-medium text-ink">Your Co-Captain</h3>
        {mine ? (
          <div className="mt-2 flex flex-wrap items-center gap-2">
            <span className="num text-ink">{mine.reviewer}</span>
            <Chip tone={mine.status === "ACTIVE" ? "info" : "plain"}>{mine.status === "ACTIVE" ? "Active" : "Invited, not accepted yet"}</Chip>
            <Button onClick={() => run("revoke", cocaptainApi.revoke)} disabled={busy === "revoke"}>Remove</Button>
          </div>
        ) : (
          <form className="mt-2 flex flex-wrap items-center gap-2" onSubmit={(e) => { e.preventDefault(); if (who.trim()) void run("invite", () => cocaptainApi.invite(who)).then(() => setWho("")); }}>
            <input aria-label="Your Co-Captain's name or email" className="min-w-0 flex-1 rounded border border-strong bg-surface px-2 py-1.5" placeholder="Their name or email" value={who} onChange={(e) => setWho(e.target.value)} />
            <Button variant="primary" type="submit" disabled={!who.trim() || busy === "invite"}>Invite</Button>
          </form>
        )}
        {!mine && <p className="mt-2 text-xs text-muted">Until you add one, going past your own limits only shows a warning, as before.</p>}
        {status.blocks_without_reviewer && !mine && <p className="mt-1 text-xs text-muted">This server pauses orders that are past your limits until you have a Co-Captain.</p>}
      </section>

      {status.invitation_from && (
        <Banner tone="info" role="status">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <span><strong className="font-semibold">{status.invitation_from}</strong> asked you to be their Co-Captain.</span>
            <Button variant="primary" onClick={() => run("accept", cocaptainApi.accept)} disabled={busy === "accept"}>Accept</Button>
          </div>
        </Banner>
      )}

      {status.reviewing.length > 0 && (
        <section aria-label="Waiting for your review" className="space-y-2">
          <h3 className="font-medium text-ink">Waiting for your review{inbox.length ? ` (${inbox.length})` : ""}</h3>
          {inbox.length === 0 && <Empty title="Nothing to review right now.">When {status.reviewing.join(", ")} goes past a limit, the order waits here for you.</Empty>}
          {inbox.map((o) => (
            <article key={o.id} aria-label={actionTitle(o)} className="rounded-xl border border-line bg-surface p-3">
              <h4 className="font-serif text-lg text-ink">{actionTitle(o)}</h4>
              <p className="text-muted">{priceLine(o)} &middot; asked {clock(o.created_at)}</p>
              <p className="mt-2 text-ink">This is past limits they set for themselves:</p>
              <ul className="list-disc pl-5 text-muted">{o.co_reasons.map((r) => <li key={r}>{r}</li>)}</ul>
              <p className="mt-2 text-xs text-muted">Nothing is sent unless you approve this exact order. It also re-checks their hard stops and locks.</p>
              <div className="mt-3 flex gap-2">
                <Button onClick={() => review(o.id, o.order_hash, false)} disabled={busy === o.id}>Decline</Button>
                <Button variant="primary" onClick={() => review(o.id, o.order_hash, true)} disabled={busy === o.id}>Approve this order</Button>
              </div>
            </article>
          ))}
        </section>
      )}
    </div>
  );
}
