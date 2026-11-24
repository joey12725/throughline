import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

// The built app is served by the Throughline API process under /ui/ from
// src/throughline/web/static (never "dist" or "build": those are git-ignored
// and hatchling would drop them from the wheel).
const api = process.env.THROUGHLINE_URL ?? "http://127.0.0.1:8801";

export default defineConfig({
  base: "/ui/",
  plugins: [react()],
  resolve: { alias: { "@": new URL("./src", import.meta.url).pathname } },
  build: {
    outDir: "../src/throughline/web/static",
    emptyOutDir: true,
    assetsDir: "assets",
    sourcemap: false,
    target: "es2022",
    chunkSizeWarningLimit: 900,
    // Never inline fonts as data: URIs. The shell's CSP is font-src 'self', so an
    // inlined data:font/woff2 is blocked; emit every font as a same-origin file.
    // Other small assets may still inline under the default 4 KiB threshold.
    assetsInlineLimit: (filePath: string) => {
      if (/\.(woff2?|ttf|otf|eot)(\?.*)?$/i.test(filePath)) return false;
      return undefined;
    },
  },
  server: { host: "127.0.0.1", port: 5173, strictPort: false, proxy: { "/v1": { target: api, changeOrigin: false } } },
  test: { environment: "node", include: ["src/**/*.test.ts", "src/**/*.test.tsx"] },
});
