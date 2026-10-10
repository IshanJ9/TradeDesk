// The signed-in trader: who they are, change password, log out. Opens as a dialog from the desk header.
import { useEffect, useRef, useState, type FormEvent } from "react";
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

        <form onSubmit={(e) => void submit(e)} className="mt-4 flex flex-col gap-3" noValidate>
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
