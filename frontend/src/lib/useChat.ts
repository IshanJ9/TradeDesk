import { useCallback, useRef, useState } from "react";
import { api } from "./api";
import type { Card } from "./types";

export interface Msg {
  id: number;
  role: "user" | "assistant";
  text: string;
  cards: Card[];
  failed?: boolean;
}

/** The conversation. The server keeps its own memory; this is only what is shown on screen. */
export function useChat() {
  const [messages, setMessages] = useState<Msg[]>([]);
  const [busy, setBusy] = useState(false);
  const next = useRef(1);
  const busyRef = useRef(false);

  const send = useCallback(async (raw: string, viaVoice = false) => {
    const text = raw.trim();
    if (!text || busyRef.current) return false;
    busyRef.current = true;
    setBusy(true);
    setMessages((m) => [...m, { id: next.current++, role: "user", text, cards: [] }]);
    const r = await api.chat(text, viaVoice);
    setMessages((m) => [
      ...m,
      r.ok
        ? { id: next.current++, role: "assistant", text: r.data.text, cards: r.data.cards }
        : { id: next.current++, role: "assistant", text: r.message, cards: [], failed: true },
    ]);
    busyRef.current = false;
    setBusy(false);
    return true;
  }, []);

  return { messages, busy, send };
}

/** "buy 10 tata" + the trader picks "Tata Motors Ltd" -> "buy 10 Tata Motors Ltd". */
export function resolveAmbiguity(previous: string | undefined, query: string, chosen: string): string {
  if (!previous) return chosen;
  const escaped = query.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const replaced = previous.replace(new RegExp(escaped, "i"), chosen);
  return replaced === previous ? `${previous} ${chosen}` : replaced;
}
