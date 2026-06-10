import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// In dev, proxy the API + SSE to the in-process aiohttp dashboard server.
// DASHBOARD_PORT defaults to 8770 (config/settings.py). Override with
// VITE_API_TARGET if you run the backend elsewhere.
const target = process.env.VITE_API_TARGET || "http://127.0.0.1:8770";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": { target, changeOrigin: true },
      "/events": { target, changeOrigin: true, ws: false },
    },
  },
  build: { outDir: "dist", emptyOutDir: true },
});
