import { useEffect, type Dispatch } from "react";
import type { Action, Toast } from "../lib/store";
import { Button } from "./ui";

function ToastItem({ toast, onClose, onReveal }: { toast: Toast; onClose: () => void; onReveal: (id: string) => void }) {
  // rule alerts stay until read; errors and notes clear themselves
  useEffect(() => {
    if (toast.kind === "rule") return;
    const t = setTimeout(onClose, 8000);
    return () => clearTimeout(t);
  }, [toast.kind, onClose]);

  const tone = toast.kind === "error" ? "border-[var(--error-line)] bg-[var(--error-bg)] text-[var(--error-ink)]" : "border-strong bg-paper text-ink";
  return (
    <div role="status" data-motion className={`rounded-xl border px-3.5 py-3 text-[13px] shadow-[var(--shadow)] ${tone}`} style={{ animation: "toast-in 200ms ease-out both" }}>
      {toast.kind === "rule" && <p className="mb-1 text-[11px] font-medium uppercase tracking-[0.08em] text-muted">Standing rule</p>}
      <p className="whitespace-pre-line leading-snug">{toast.message}</p>
      <div className="mt-2 flex justify-end gap-2">
        {toast.cardId && <Button className="px-2.5 py-1 text-xs" onClick={() => { onReveal(toast.cardId!); onClose(); }}>Review ticket</Button>}
        <Button variant="plain" className="px-2.5 py-1 text-xs" onClick={onClose}>Dismiss</Button>
      </div>
    </div>
  );
}

export function Toasts({ toasts, dispatch, onReveal }: { toasts: Toast[]; dispatch: Dispatch<Action>; onReveal: (id: string) => void }) {
  if (!toasts.length) return null;
  return (
    <div className="pointer-events-none fixed bottom-16 right-4 z-50 lg:bottom-4 flex w-[min(24rem,calc(100vw-2rem))] flex-col gap-2 [&>*]:pointer-events-auto">
      {toasts.map((t) => (
        <ToastItem key={t.id} toast={t} onClose={() => dispatch({ type: "dismissToast", id: t.id })} onReveal={onReveal} />
      ))}
    </div>
  );
}
