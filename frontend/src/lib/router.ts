// A tiny router for four pages; no library needed. Paths are real URLs (the dev server serves index.html for all).
import { useEffect, useState } from "react";

export type Route = "landing" | "how" | "login" | "app";

const PATHS: Record<Route, string> = { landing: "/", how: "/how-it-works", login: "/login", app: "/app" };

export function pathOf(route: Route): string {
  return PATHS[route];
}

export function routeOf(pathname: string): Route {
  const path = pathname.replace(/\/+$/, "") || "/";
  const found = (Object.keys(PATHS) as Route[]).find((r) => PATHS[r] === path);
  return found ?? "landing";
}

export function navigate(route: Route): void {
  if (window.location.pathname !== PATHS[route]) window.history.pushState({}, "", PATHS[route]);
  window.dispatchEvent(new PopStateEvent("popstate"));
  window.scrollTo(0, 0);
}

export function useRoute(): Route {
  const [route, setRoute] = useState<Route>(() => routeOf(window.location.pathname));
  useEffect(() => {
    const onPop = () => setRoute(routeOf(window.location.pathname));
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, []);
  return route;
}
