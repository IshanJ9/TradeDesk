import { useEffect, useRef, useState, type KeyboardEvent } from "react";
import { actionTitle } from "../lib/describe";
import { rupees } from "../lib/format";
import { resolveAmbiguity, type Msg } from "../lib/useChat";
import type { Card } from "../lib/types";
import { api } from "../lib/api";
import { Banner, Button, Chip } from "./ui";

const SUGGESTIONS = [
  "What's my P&L today and which positions are down more than 5%?",
  "Buy 10 Infosys at 1450",
  "Sell half my Infosys and buy ITC with the money",
  "Buy 5 TCS if it falls below 3800",
  "Alert me if HDFC Bank drops 3% from my buy price",
  "Show me NIFTY options near the money",
];

interface Props {
  messages: Msg[];
  busy: boolean;
  offline: boolean;
  send: (text: string) => Promise<boolean>;
  onReveal: (id: string) => void;
}

function CardView({ card, onReveal, onPick }: { card: Card; onReveal: (id: string) => void; onPick: (query: string, name: string) => void }) {
  switch (card.type) {
    case "notice": {
      const tone = card.level === "blocked" ? "error" : card.level === "warning" ? "warn" : "info";
      return <Banner tone={tone} role="status">{card.message}</Banner>;
    }
    case "ambiguity":
      return (
        <div className="rounded-xl border border-line bg-paper p-3">
          <p className="mb-2 text-[13px] text-muted">Pick the one you mean:</p>
          <div className="flex flex-wrap gap-2">
            {card.candidates.map((c) => (
              <Button key={c.symbol} onClick={() => onPick(card.query, c.name || c.symbol)}>
                <span className="num">{c.symbol}</span>
                <span className="text-muted">{c.name}</span>
              </Button>
            ))}
          </div>
        </div>
      );
    case "rule":
      return (
        <div className="flex flex-wrap items-center gap-2 rounded-xl border border-line bg-paper px-3 py-2 text-[13px]">
          <Chip tone="info">Rule saved</Chip>
          <Chip>{card.rule.kind === "ALERT" ? "Alert" : "Order rule"}</Chip>
          <span className="num text-xs text-muted">Trigger {rupees(card.rule.condition.trigger_price)}</span>
          <span className="flex-1" />
          <Button variant="plain" className="px-2 py-1 text-xs" onClick={() => api.cancelRule(card.rule.id)}>Cancel rule</Button>
        </div>
      );
    case "pending_order":
      return (
        <Button className="w-full justify-between" onClick={() => onReveal(card.pending.id)}>
          <span><span className="num font-medium">{actionTitle(card.pending)}</span> is ready to review</span>
          <span aria-hidden="true">&rarr;</span>
        </Button>
      );
    case "plan":
      return (
        <Button className="w-full justify-between" onClick={() => onReveal(card.plan.id)}>
          <span><span className="font-medium">{card.plan.title}</span> is ready to review</span>
          <span aria-hidden="true">&rarr;</span>
        </Button>
      );
  }
}

export function ChatPanel({ messages, busy, offline, send, onReveal }: Props) {
  const [text, setText] = useState("");
  const endRef = useRef<HTMLDivElement>(null);
  const fieldRef = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [messages, busy]);

  const submit = async (value = text) => {
    if (!value.trim() || busy) return;
    setText("");
    await send(value);
    fieldRef.current?.focus();
  };

  const onKey = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault();
      void submit();
    }
  };

  const pick = (query: string, name: string) => {
    const lastUser = [...messages].reverse().find((m) => m.role === "user");
    void send(resolveAmbiguity(lastUser?.text, query, name));
  };

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="flex-1 overflow-y-auto px-4 pb-2 pt-4 scroll-quiet" role="log" aria-live="polite" aria-label="Conversation">
        {messages.length === 0 ? (
          <div className="mx-auto max-w-[34rem] pt-6">
            <h1 className="font-serif text-[34px] italic leading-[1.05] text-ink">Ask about your account, or describe an order.</h1>
            <p className="mt-3 text-[15px] text-muted">
              I read your data and prepare orders. I never send one: every order waits on your desk as a ticket until you approve that exact ticket.
            </p>
            <p className="mt-6 text-xs font-medium uppercase tracking-[0.08em] text-muted">Try one of these</p>
            <ul className="mt-2 space-y-1.5">
              {SUGGESTIONS.map((s) => (
                <li key={s}>
                  <button
                    onClick={() => void submit(s)}
                    disabled={busy}
                    className="w-full rounded-lg border border-line bg-paper px-3 py-2 text-left text-[13px] text-ink transition-colors hover:border-strong hover:bg-surface2 disabled:opacity-50"
                  >
                    {s}
                  </button>
                </li>
              ))}
            </ul>
          </div>
        ) : (
          <ul className="mx-auto flex max-w-[40rem] flex-col gap-4">
            {messages.map((m) =>
              m.role === "user" ? (
                <li key={m.id} className="flex justify-end">
                  <p className="max-w-[85%] whitespace-pre-line rounded-2xl rounded-br-md bg-ink px-3.5 py-2 text-[14px] text-inverse">{m.text}</p>
                </li>
              ) : (
                <li key={m.id} className="space-y-2">
                  {m.failed ? (
                    <Banner tone="error" role="alert">{m.text}</Banner>
                  ) : (
                    <p className="whitespace-pre-line text-[14px] leading-relaxed text-ink">{m.text}</p>
                  )}
                  {m.cards.map((c, i) => (
                    <CardView key={i} card={c} onReveal={onReveal} onPick={pick} />
                  ))}
                </li>
              ),
            )}
            {busy && (
              <li className="flex items-center gap-1 text-muted" aria-label="Working on it">
                {[0, 1, 2].map((i) => (
                  <span key={i} data-motion className="h-1.5 w-1.5 rounded-full bg-muted" style={{ animation: `dots 1s ${i * 0.15}s infinite` }} />
                ))}
              </li>
            )}
          </ul>
        )}
        <div ref={endRef} />
      </div>

      <form
        onSubmit={(e) => { e.preventDefault(); void submit(); }}
        className="border-t border-line bg-surface px-4 py-3"
      >
        <div className="mx-auto flex max-w-[40rem] items-end gap-2">
          <label htmlFor="ask" className="sr-only">Ask or describe an order</label>
          <textarea
            id="ask"
            ref={fieldRef}
            value={text}
            onChange={(e) => setText(e.target.value)}
            onKeyDown={onKey}
            rows={1}
            placeholder={offline ? "Reconnecting… you can still type" : "Ask, or describe an order"}
            className="max-h-32 min-h-[42px] flex-1 resize-none rounded-xl border border-strong bg-paper px-3.5 py-2.5 text-[14px] text-ink placeholder:text-muted"
          />
          <Button variant="primary" type="submit" disabled={busy || !text.trim()} className="h-[42px]">
            Send
          </Button>
        </div>
        <p className="mx-auto mt-1.5 max-w-[40rem] text-[11px] text-muted">
          Enter to send &middot; Shift+Enter for a new line &middot; Facts from your account, not advice.
        </p>
      </form>
    </div>
  );
}
