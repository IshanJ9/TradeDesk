// Co-Captain state for the desk: who the trader's Co-Captain is, an invitation waiting for this person, and the cards
// waiting for THEIR review. Read by polling (a GET never changes anything); every change is a deliberate POST.
import { useCallback, useEffect, useRef, useState } from "react";
import { cocaptainApi, type CoCaptainStatus } from "./api";
import type { PendingOrder } from "./types";

const POLL_MS = 5000;

export interface CoCaptainView {
  status: CoCaptainStatus | null;
  inbox: PendingOrder[];
  /** how many things need this person's attention: an invitation, or cards to review */
  attention: number;
  refresh: () => Promise<void>;
}

export function useCoCaptain(): CoCaptainView {
  const [status, setStatus] = useState<CoCaptainStatus | null>(null);
  const [inbox, setInbox] = useState<PendingOrder[]>([]);
  const alive = useRef(true);

  const refresh = useCallback(async () => {
    const [s, i] = await Promise.all([cocaptainApi.status(), cocaptainApi.inbox()]);
    if (!alive.current) return;
    if (s.ok) setStatus(s.data);
    if (i.ok) setInbox(i.data);
  }, []);

  useEffect(() => {
    alive.current = true;
    void refresh();
    const timer = setInterval(() => { if (!document.hidden) void refresh(); }, POLL_MS);
    return () => { alive.current = false; clearInterval(timer); };
  }, [refresh]);

  return { status, inbox, attention: inbox.length + (status?.invitation_from ? 1 : 0), refresh };
}
