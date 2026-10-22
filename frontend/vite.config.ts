import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Dev: vite serves on :5174 with a proxy to the isolated hunter/gatherer demo on :8767.
// Build: emits the library without replacing the legacy workbench's assets.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5174,
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8767",
        changeOrigin: true,
        configure: (proxy) => {
          proxy.on("proxyReq", (outgoing, incoming) => {
            // Only translate the local Vite origin; preserve rejection of foreign origins.
            if (["http://localhost:5174", "http://127.0.0.1:5174"].includes(incoming.headers.origin ?? "")) {
              outgoing.setHeader("Origin", "http://127.0.0.1:8767");
            }
          });
        },
      },
    },
  },
  build: {
    outDir: "../hunter_gatherer/server/library_static",
    emptyOutDir: true,
  },
});
