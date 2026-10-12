import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

const RUNTIME = process.env.RUNTIME_URL ?? "http://localhost:8000";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": RUNTIME,
      "/ws": { target: RUNTIME.replace("http", "ws"), ws: true },
    },
  },
});
