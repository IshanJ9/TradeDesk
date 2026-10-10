// The signed-in trader: who they are, change password, log out. Opens as a dialog from the desk header.
import { useEffect, useRef, useState, type FormEvent } from "react";
import { linkBroker, reconnectBroker, refreshBroker, unlinkBroker, useBroker } from "../lib/broker";
import { changePassword, signOut, useSession } from "../lib/session";

function Person() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <circle cx="12" cy="8" r="4" />
      <path d="M4 21c0-4 3.6-7 8-7s8 3 8 7" />
    </svg>
  );
}

export function AccountMenu() {
  const { user } = useSession();
  const [open, setOpen] = useState(false);
  const opener = useRef<HTMLButtonElement>(null);
  if (!user) return null;
  const close = () => { setOpen(false); opener.current?.focus(); };
  return (
    <>
      <button
        ref={opener}
        type="button"
        onClick={() => setOpen(true)}
        aria-haspopup="dialog"
        aria-label={`Account: ${user.display_name || user.email}`}
        className="inline-flex min-h-[36px] max-w-[160px] items-center gap-1.5 rounded-lg px-2.5 text-xs text-muted hover:bg-surface2"
      >
        <Person />
        <span className="hidden truncate sm:inline">{user.display_name || user.email}</span>
      </button>
      {open && <AccountDialog onClose={close} />}
    </>
  );
}

function AccountDialog({ onClose }: { onClose: () => void }) {
  const { user } = useSession();
  const [message, setMessage] = useState<{ tone: "ok" | "error"; text: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const first = useRef<HTMLInputElement>(null);

  useEffect(() => {
    first.current?.focus();
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  async function submit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const form = e.currentTarget;
    const data = new FormData(form);
    const current = String(data.get("current") ?? "");
    const next = String(data.get("next") ?? "");
    if (!current || !next) { setMessage({ tone: "error", text: "Enter your current and your new password." }); return; }
    setBusy(true);
    setMessage(null);
    const r = await changePassword(current, next);
    setBusy(false);
    if (r.ok) { form.reset(); setMessage({ tone: "ok", text: "Password changed. You've been signed out of your other devices." }); }
    else setMessage({ tone: "error", text: r.message });
  }

  return (
    <div className="fixed inset-0 z-50 grid place-items-center bg-black/40 px-4" onMouseDown={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <div role="dialog" aria-modal="true" aria-labelledby="account-title" className="max-h-[90dvh] w-full max-w-[420px] overflow-y-auto rounded-xl border border-line bg-surface p-5 shadow-xl">
        <div className="flex items-start justify-between gap-3">
          <div className="min-w-0">
            <h2 id="account-title" className="m-0 text-base font-semibold">Your account</h2>
            <p className="m-0 mt-0.5 truncate text-sm text-muted">{user?.email}</p>
          </div>
          <button type="button" onClick={onClose} aria-label="Close" className="min-h-[36px] min-w-[36px] rounded-lg text-muted hover:bg-surface2">✕</button>
        </div>

        <BrokerSection />
        <hr className="my-4 border-line" />

        <form onSubmit={(e) => void submit(e)} className="flex flex-col gap-3" noValidate>
          <h3 className="m-0 text-sm font-semibold">Change password</h3>
          <label className="flex flex-col gap-1 text-[13px]">Current password
            <input ref={first} name="current" type="password" autoComplete="current-password" className="min-h-[40px] rounded-lg border border-line bg-surface px-3 text-sm" />
          </label>
          <label className="flex flex-col gap-1 text-[13px]">New password
            <input name="next" type="password" autoComplete="new-password" className="min-h-[40px] rounded-lg border border-line bg-surface px-3 text-sm" />
            <span className="text-xs text-muted">At least 10 characters. Changing it signs you out everywhere else.</span>
          </label>
          {message && (
            <div role={message.tone === "error" ? "alert" : "status"} className={`rounded-lg px-3 py-2 text-[13px] ${message.tone === "error" ? "bg-[var(--warn-bg)] text-loss" : "bg-surface2 text-ink"}`}>
              {message.text}
            </div>
          )}
          <button type="submit" disabled={busy} className="min-h-[40px] rounded-lg bg-ink px-4 text-sm font-medium text-inverse disabled:opacity-60">{busy ? "One moment…" : "Change password"}</button>
        </form>

        <hr className="my-4 border-line" />
        <button type="button" onClick={() => void signOut()} className="min-h-[40px] w-full rounded-lg border border-line px-4 text-sm font-medium hover:bg-surface2">Log out</button>
      </div>
    </div>
  );
}

/** The broker account this desk trades on: the simulated one, or the user's own 021 account. */
function BrokerSection() {
  const broker = useBroker();
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [confirmUnlink, setConfirmUnlink] = useState(false);
  useEffect(() => { void refreshBroker(); }, []);
  if (!broker) return <p className="mt-4 text-sm text-muted">Checking your broker account…</p>;

  async function run(call: () => Promise<{ ok: true } | { ok: false; message: string }>) {
    setBusy(true);
    setMessage(null);
    const r = await call();
    setBusy(false);
    setConfirmUnlink(false);
    if (!r.ok) setMessage(r.message);
  }

  async function link(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const form = e.currentTarget;
    const data = new FormData(form);
    const username = String(data.get("ucc") ?? "").trim();
    const password = String(data.get("pw021") ?? "");
    if (!username || !password) { setMessage("Enter your 021 client id and password."); return; }
    await run(() => linkBroker(username, password));
    form.reset();
  }

  return (
    <section className="mt-4 flex flex-col gap-3" aria-labelledby="broker-title">
      <h3 id="broker-title" className="m-0 text-sm font-semibold">021 account</h3>
      {broker.server_account ? (
        <p className="m-0 text-[13px] text-muted">This desk trades on the 021 login set on the server{broker.ucc_hint ? ` (client id ${broker.ucc_hint})` : ""}. It is managed there, not here.</p>
      ) : broker.kind === "021" ? (
        <>
          <p className="m-0 text-[13px]">
            Linked: client id {broker.ucc_hint}. {broker.status === "needs_reconnect" ? <strong>Not connected right now.</strong> : "Connected."}
          </p>
          <div className="flex flex-wrap gap-2">
            {broker.status === "needs_reconnect" && (
              <button type="button" disabled={busy} onClick={() => void run(reconnectBroker)} className="min-h-[40px] rounded-lg bg-ink px-4 text-sm font-medium text-inverse disabled:opacity-60">Reconnect</button>
            )}
            {confirmUnlink ? (
              <button type="button" disabled={busy} onClick={() => void run(unlinkBroker)} className="min-h-[40px] rounded-lg border border-line px-4 text-sm font-medium text-loss disabled:opacity-60">Yes, unlink and cancel waiting orders</button>
            ) : (
              <button type="button" disabled={busy} onClick={() => setConfirmUnlink(true)} className="min-h-[40px] rounded-lg border border-line px-4 text-sm font-medium hover:bg-surface2">Unlink</button>
            )}
          </div>
          <p className="m-0 text-xs text-muted">Unlinking deletes the saved login and returns you to the simulated account. Orders waiting for your approval are cancelled.</p>
        </>
      ) : broker.can_link ? (
        <form onSubmit={(e) => void link(e)} className="flex flex-col gap-3" noValidate>
          <p className="m-0 text-[13px] text-muted">You're on the simulated account (no real money). To trade on your own 021 account, link it: your login is checked with 021 and then stored encrypted. Linking cancels any orders waiting for your approval.</p>
          <label className="flex flex-col gap-1 text-[13px]">021 client id
            <input name="ucc" autoComplete="off" autoCapitalize="characters" className="min-h-[40px] rounded-lg border border-line bg-surface px-3 text-sm" />
          </label>
          <label className="flex flex-col gap-1 text-[13px]">021 password
            <input name="pw021" type="password" autoComplete="off" className="min-h-[40px] rounded-lg border border-line bg-surface px-3 text-sm" />
          </label>
          <button type="submit" disabled={busy} className="min-h-[40px] rounded-lg border border-line px-4 text-sm font-medium hover:bg-surface2 disabled:opacity-60">{busy ? "Checking with 021…" : "Link my 021 account"}</button>
        </form>
      ) : (
        <p className="m-0 text-[13px] text-muted">You're on the simulated account (no real money). {broker.link_unavailable_reason}</p>
      )}
      {message && <div role="alert" className="rounded-lg bg-[var(--warn-bg)] px-3 py-2 text-[13px] text-loss">{message}</div>}
    </section>
  );
}
