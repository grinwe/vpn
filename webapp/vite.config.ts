import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Bundle is served from /app/ behind nginx, so all asset URLs need that
// prefix baked in at build time.
export default defineConfig({
  plugins: [react()],
  base: "/app/",
  build: {
    outDir: "dist",
    sourcemap: true,
  },
  server: {
    port: 5174,
    proxy: {
      "/api": "http://localhost:8000",
    },
  },
});
