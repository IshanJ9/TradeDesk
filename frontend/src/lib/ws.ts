// The live feed. One WebSocket; the server sends a snapshot first, then events with a global,
// increasing `seq`. If a seq is ever skipped we drop the connection and reconnect, which gives a
// fresh snapshot, so the screen can never silently drift away from the server.
import { useEffect } from "react";
import type { Action } from "./store";
import type { WsEvent } from "./types";

export function wsUrl(loc: Pick<Location, "protocol" | "host"> = window.location): string {
  return `${loc.protocol === "https:" ? "wss" : "ws"}://${loc.host}/ws`;
}

/** True when `next` follows `last` with nothing missed. A snapshot always starts a new sequence. */
export function isContiguous(last: number | null, event: Pick<WsEvent, "type" | "seq">): boolean {
  return event.type === "snapshot" || last === null || event.seq === last + 1;
}

export function useLiveFeed(dispatch: (a: Action) => void): void {
  useEffect(() => {
    let socket: WebSocket | null = null;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let attempt = 0;
    let stopped = false;

    const connect = () => {
      dispatch({ type: "conn", status: "connecting" });
      // a demo dev actor (?as=a) rides along as a subprotocol pair; a real session will not need this
      const as = new URLSearchParams(location.search).get("as");
      socket = as ? new WebSocket(wsUrl(), ["tradedesk-cocaptain", as]) : new WebSocket(wsUrl());
      let last: number | null = null;

      socket.onopen = () => {
        attempt = 0;
      };
      socket.onmessage = (msg) => {
        let event: WsEvent;
        try {
          event = JSON.parse(msg.data) as WsEvent;
        } catch {
          return;
        }
        if (!isContiguous(last, event)) {
          socket?.close(); // resync from a fresh snapshot
          return;
        }
        last = event.seq;
        if (event.type === "snapshot") dispatch({ type: "conn", status: "live" });
        dispatch({ type: "event", event });
      };
      socket.onclose = () => {
        if (stopped) return;
        dispatch({ type: "conn", status: "offline" });
        attempt += 1;
        timer = setTimeout(connect, Math.min(1000 * 2 ** Math.min(attempt, 3), 8000));
      };
      socket.onerror = () => socket?.close();
    };

    connect();
    return () => {
      stopped = true;
      if (timer) clearTimeout(timer);
      socket?.close();
    };
  }, [dispatch]);
}
