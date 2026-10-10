// Co-Captain: a second person who must also approve an order, but only when the trader is past a limit they set.
// One panel for both roles: the trader invites and removes their Co-Captain; the Co-Captain accepts and reviews.
import { useState, type Dispatch } from "react";
import { cocaptainApi, type CoCaptainLink } from "../lib/api";
import { invitationFor, type CoCaptainView } from "../lib/cocaptain";
import { actionTitle, priceLine } from "../lib/describe";
import { clock } from "../lib/format";
import type { Action } from "../lib/store";
import { Banner, Button, Chip, Empty } from "./ui";

export function CoCaptainPanel({ view, dispatch }: { view: CoCaptainView; dispatch: Dispatch<Action> }) {
  const { enabled, settings, inbox, refresh } = view;
  const [email, setEmail] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [problem, setProblem] = useState<string | null>(null);

  async function run(key: string, call: () => Promise<{ ok: boolean; message?: string }>) {
    setBusy(key);
    setProblem(null);
    const r = await call();
    setBusy(null);
    if (!r.ok && r.message) setProblem(r.message);
    await refresh();
    return r.ok;
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

  if (enabled === false) {
    return <Empty title="Co-Captain is off on this server.">It is a second person who approves an order too, only when you are past a limit you set. The operator switches it on.</Empty>;
  }
  if (!settings) return <Empty title="Loading…">Checking your Co-Captain settings.</Empty>;

  const me = settings.actor;
  const name = (id: string) => settings.people.find((p) => p.id === id)?.display_name ?? id;
  const mine: CoCaptainLink | undefined = settings.links.find((l) => l.owner_id === me.id && l.status !== "REVOKED");
  const invitation = invitationFor(settings);
  const reviewing = settings.links.filter((l) => l.reviewer_id === me.id && l.status === "ACTIVE");

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
            <span className="num text-ink">{name(mine.reviewer_id)}</span>
            <Chip tone={mine.status === "ACTIVE" ? "info" : "plain"}>{mine.status === "ACTIVE" ? "Active" : "Invited, not accepted yet"}</Chip>
            <Button onClick={() => run("revoke", () => cocaptainApi.revoke(mine))} disabled={busy === "revoke"}>Remove</Button>
          </div>
        ) : (
          <form className="mt-2 flex flex-wrap items-center gap-2" onSubmit={(e) => { e.preventDefault(); if (email.trim()) void run("invite", () => cocaptainApi.invite(email.trim())).then((ok) => ok && setEmail("")); }}>
            <input aria-label="Your Co-Captain's email" type="email" className="min-w-0 flex-1 rounded border border-strong bg-surface px-2 py-1.5" placeholder="Their email" value={email} onChange={(e) => setEmail(e.target.value)} />
            <Button variant="primary" type="submit" disabled={!email.trim() || busy === "invite"}>Invite</Button>
          </form>
        )}
        {!mine && <p className="mt-2 text-xs text-muted">Without one, an order that is past your own limits is paused until you add one or are back inside them.</p>}
        {!settings.limits_configured && <p className="mt-1 text-xs text-muted">You haven&rsquo;t saved any Discipline limits yet, so no order is ever past one and a Co-Captain has nothing to do.</p>}
      </section>

      {invitation && (
        <Banner tone="info" role="status">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <span><strong className="font-semibold">{name(invitation.owner_id)}</strong> asked you to be their Co-Captain.</span>
            <div className="flex gap-2">
              <Button onClick={() => run("decline-invite", () => cocaptainApi.revoke(invitation))} disabled={busy === "decline-invite"}>Not now</Button>
              <Button variant="primary" onClick={() => run("accept", () => cocaptainApi.accept(invitation))} disabled={busy === "accept"}>Accept</Button>
            </div>
          </div>
        </Banner>
      )}

      {reviewing.length > 0 && (
        <section aria-label="Waiting for your review" className="space-y-2">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <h3 className="font-medium text-ink">Waiting for your review{inbox.length ? ` (${inbox.length})` : ""}</h3>
            {reviewing.map((l) => (
              <Button key={l.id} onClick={() => run("leave", () => cocaptainApi.revoke(l))} disabled={busy === "leave"}>Stop being {name(l.owner_id)}&rsquo;s Co-Captain</Button>
            ))}
          </div>
          {inbox.length === 0 && <Empty title="Nothing to review right now.">When {reviewing.map((l) => name(l.owner_id)).join(", ")} goes past a limit, the order waits here for you.</Empty>}
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
