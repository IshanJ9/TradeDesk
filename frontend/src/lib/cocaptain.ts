// Co-Captain state for the desk: whether the feature is on, who is pairing with whom, and the cards waiting for THIS
// person's review. Read by polling (a GET never changes anything); every change is a deliberate POST.
import { useCallback, useEffect, useRef, useState } from "react";
import { cocaptainApi, type CoCaptainLink, type CoCaptainSettings } from "./api";
import type { PendingOrder } from "./types";

const POLL_MS = 5000;

export interface CoCaptainView {
  /** null until the first answer; false when the server has Co-Captain switched off */
  enabled: boolean | null;
  settings: CoCaptainSettings | null;
  inbox: PendingOrder[];
  /** how many things need this person's attention: an invitation, or cards to review */
  attention: number;
  refresh: () => Promise<void>;
}

/** The invitation addressed to this person that they have not accepted yet. */
export function invitationFor(s: CoCaptainSettings | null): CoCaptainLink | undefined {
  return s?.links.find((l) => l.reviewer_id === s.actor.id && l.status === "INVITED");
}

/** null while we find out; true when Co-Captain is on and the caller is not the account's trader. */
export function useIsReviewer(): boolean | null {
  const [reviewer, setReviewer] = useState<boolean | null>(null);
  useEffect(() => {
    let live = true;
    void (async () => {
      const c = await cocaptainApi.config();
      if (!c.ok || !c.data.enabled) return live && setReviewer(false);
      const s = await cocaptainApi.settings();
      if (live) setReviewer(s.ok && s.data.actor.id !== s.data.account_owner_id);
    })();
    return () => { live = false; };
  }, []);
  return reviewer;
}

export function useCoCaptain(): CoCaptainView {
  const [enabled, setEnabled] = useState<boolean | null>(null);
  const [settings, setSettings] = useState<CoCaptainSettings | null>(null);
  const [inbox, setInbox] = useState<PendingOrder[]>([]);
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
    const [s, i] = await Promise.all([cocaptainApi.settings(), cocaptainApi.inbox()]);
    if (!alive.current) return;
    if (s.ok) setSettings(s.data);
    if (i.ok) setInbox(i.data);
  }, []);

  useEffect(() => {
    alive.current = true;
    void refresh();
    const timer = setInterval(() => { if (!document.hidden) void refresh(); }, POLL_MS);
    return () => { alive.current = false; clearInterval(timer); };
  }, [refresh]);

  const attention = inbox.length + (invitationFor(settings) ? 1 : 0);
  return { enabled, settings, inbox, attention, refresh };
}
