import { defineConfig } from "vite";

/** Standard Vite app: build → web/dist; CI copies dist into the Python wheel via scripts/bundle_report_player.py */
export default defineConfig({
  root: ".",
  base: "/",
  publicDir: "public",
  server: {
    port: 5173,
    proxy: {
      "/api": "http://127.0.0.1:8765",
      "/runs": "http://127.0.0.1:8765",
    },
  },
  build: {
    outDir: "dist",
    emptyOutDir: true,
    sourcemap: true,
    assetsDir: "assets",
  },
});
