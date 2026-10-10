import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import "@fontsource/libre-baskerville/400.css";
import "@fontsource/libre-baskerville/400-italic.css";
import "@fontsource/libre-baskerville/700.css";
import App from "./App";
import { HowItWorks } from "./brand/HowItWorks";
import { Landing } from "./brand/Landing";
import { Login } from "./brand/Login";
import "./brand/brand.css";
import "./index.css";
import { applyTheme, readTheme } from "./lib/theme";
import { navigate, useRoute } from "./lib/router";
import { loadSession, useSession } from "./lib/session";
import { useEffect } from "react";

applyTheme(readTheme()); // before the first paint, so a chosen theme never flashes

const TITLES = { landing: "TradeDesk · Copilot, not autopilot", how: "How TradeDesk works", login: "Log in · TradeDesk", app: "Desk · TradeDesk" };

function Root() {
  const route = useRoute();
  const session = useSession();
  document.title = TITLES[route];
  useEffect(() => { void loadSession(); }, []); // who is signed in? (the cookie says; the server answers)
  useEffect(() => {
    if (route === "app" && session.status === "signedOut") navigate("login"); // the desk needs a signed-in user
  }, [route, session.status]);
  if (route === "app") {
    if (session.status !== "signedIn") return <div className="grid min-h-dvh place-items-center text-sm text-muted" role="status">Opening your desk…</div>;
    return <App key={session.user?.id} />; // the desk: opens the live connection only here; a new user gets a fresh one
  }
  if (route === "how") return <HowItWorks />;
  if (route === "login") return <Login />;
  return <Landing />;
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <Root />
  </StrictMode>,
);
