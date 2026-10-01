import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Admin is served from /admin/ behind nginx on grinwer.online. The SPA
// therefore needs a non-root base so asset URLs resolve correctly.
export default defineConfig({
  base: "/admin/",
  plugins: [react()],
  server: {
    host: "127.0.0.1",
    port: 5173,
    proxy: {
      "/api": "http://127.0.0.1:8000",
    },
  },
  build: {
    outDir: "dist",
    sourcemap: true,
  },
});
