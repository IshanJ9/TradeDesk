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
import { useRoute } from "./lib/router";

applyTheme(readTheme()); // before the first paint, so a chosen theme never flashes

const TITLES = { landing: "TradeDesk · Copilot, not autopilot", how: "How TradeDesk works", login: "Log in · TradeDesk", app: "Desk · TradeDesk" };

function Root() {
  const route = useRoute();
  document.title = TITLES[route];
  if (route === "app") return <App />; // the desk: opens the live connection only here
  if (route === "how") return <HowItWorks />;
  if (route === "login") return <Login />;
  return <Landing />;
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <Root />
  </StrictMode>,
);
