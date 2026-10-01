import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Admin is served from /${VITE_ADMIN_BASE_PATH}/ behind nginx on
// grinwer.online. Default "admin" keeps the legacy URL; prod deploys
// pass a secret value (see deploy_web_frontend_admin_path / ansible
// vault) to make the panel disappear from sweep-scans. Rebuild is
// required on change — Vite bakes the base into every asset URL at
// build time.
const adminBasePath = process.env.VITE_ADMIN_BASE_PATH || "admin";

export default defineConfig({
  base: `/${adminBasePath}/`,
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
