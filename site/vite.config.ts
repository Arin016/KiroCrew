import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

// Public GitHub Pages build. Served from https://<org>.github.io/kiroclaw/,
// so assets resolve under the /kiroclaw/ base. Switch base to "/" if a custom
// domain is attached later.
export default defineConfig({
  base: "/kiroclaw/",
  plugins: [react(), tailwindcss()],
  server: { port: 3000 },
  build: { outDir: "dist" },
  test: { environment: "jsdom", setupFiles: ["./tests/setup.ts"], globals: true },
});
