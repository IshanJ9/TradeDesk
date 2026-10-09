import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

// In development the UI is served by Vite and talks to the FastAPI backend through this proxy,
// so the browser sees one origin (no CORS) and the WebSocket path is just /ws.
// BACKEND_PORT lets a second copy of the UI talk to a second backend (default: the usual 8000).
const backend = process.env.BACKEND_PORT ?? "8000";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    port: 5173,
    proxy: {
      "/api": `http://127.0.0.1:${backend}`,
      "/ws": { target: `ws://127.0.0.1:${backend}`, ws: true },
    },
  },
  test: { environment: "node", include: ["src/**/*.test.ts"] },
});
