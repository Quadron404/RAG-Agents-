import { defineConfig } from "vite";
import react from "@vitejs/plugin-react-swc";

const BACKEND = process.env.VITE_BACKEND || "http://127.0.0.1:8000";

// The frontend talks to the RAG Agents backend over the same paths the
// backend already exposes (REST + websockets). In dev, Vite proxies them
// to the running FastAPI server so the app uses clean relative URLs.
export default defineConfig({
  plugins: [react()],
  build: {
    // noVNC's core uses top-level await (WebCodecs feature detection), so the
    // bundle target has to be at least es2022.
    target: "es2022",
  },
  optimizeDeps: {
    // Same reason, for the dev server's dependency pre-bundling.
    esbuildOptions: { target: "es2022" },
  },
  server: {
    port: 5173,
    host: "127.0.0.1",
    proxy: {
      // /ws covers the agent session and the raw-RFB /ws/screen relay.
      "/ws": { target: BACKEND.replace(/^http/, "ws"), ws: true },
      // /auth is what mints the session cookie, so it has to be proxied too or
      // the login form would 404 in dev and the app would be unusable.
      "/auth": BACKEND,
      "/threads": BACKEND,
      "/computer": BACKEND,
      "/screen": BACKEND,
      "/sysinfo": BACKEND,
      "/file": BACKEND,
      "/cdp": BACKEND,
      "/health": BACKEND,
    },
  },
});