import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

// In development the UI is served by Vite and talks to the FastAPI backend through this proxy,
// so the browser sees one origin (no CORS) and the WebSocket path is just /ws.
export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    port: 5173,
    proxy: {
      "/api": "http://127.0.0.1:8000",
      "/ws": { target: "ws://127.0.0.1:8000", ws: true },
    },
  },
  test: { environment: "node", include: ["src/**/*.test.ts"] },
});
