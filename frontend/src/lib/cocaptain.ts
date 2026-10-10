// Co-Captain state for the desk: whether the feature is on, who is pairing with whom, and the cards waiting for THIS
// person's review. Read by polling (a GET never changes anything); every change is a deliberate POST.
import { useCallback, useEffect, useRef, useState } from "react";
import { cocaptainApi, type CoCaptainLink, type CoCaptainSettings } from "./api";
import type { PendingOrder, Plan } from "./types";
import { COCAPTAIN_EVENT } from "./ws";

const POLL_MS = 5000; // a backup: the live feed (lib/ws.ts) normally tells this desk the moment something changes

export interface CoCaptainView {
  /** null until the first answer; false when the server has Co-Captain switched off */
  enabled: boolean | null;
  settings: CoCaptainSettings | null;
  inbox: PendingOrder[];
  planInbox: Plan[];
  /** how many things need this person's attention: an invitation, or cards to review */
  attention: number;
  refresh: () => Promise<void>;
}

/** The invitation addressed to this person that they have not accepted yet. */
export function invitationFor(s: CoCaptainSettings | null): CoCaptainLink | undefined {
  return s?.links.find((l) => l.reviewer_id === s.actor.id && l.status === "INVITED");
}

export function useCoCaptain(): CoCaptainView {
  const [enabled, setEnabled] = useState<boolean | null>(null);
  const [settings, setSettings] = useState<CoCaptainSettings | null>(null);
  const [inbox, setInbox] = useState<PendingOrder[]>([]);
  const [planInbox, setPlanInbox] = useState<Plan[]>([]);
  const alive = useRef(true);
  const on = useRef<boolean | null>(null);

  const refresh = useCallback(async () => {
    if (on.current === null) {
      const c = await cocaptainApi.config();
      if (!alive.current) return;
      on.current = c.ok ? c.data.enabled : false;
      setEnabled(on.current);
    }
    if (!on.current) return;
    const [s, i, p] = await Promise.all([cocaptainApi.settings(), cocaptainApi.inbox(), cocaptainApi.planInbox()]);
    if (!alive.current) return;
    if (s.ok) setSettings(s.data);
    if (i.ok) setInbox(i.data);
    if (p.ok) setPlanInbox(p.data);
  }, []);

  useEffect(() => {
    alive.current = true;
    void refresh();
    const timer = setInterval(() => { if (!document.hidden) void refresh(); }, POLL_MS);
    const now = () => void refresh();
    window.addEventListener(COCAPTAIN_EVENT, now);
    return () => { alive.current = false; clearInterval(timer); window.removeEventListener(COCAPTAIN_EVENT, now); };
  }, [refresh]);

  const attention = inbox.length + planInbox.length + (invitationFor(settings) ? 1 : 0);
  return { enabled, settings, inbox, planInbox, attention, refresh };
}
