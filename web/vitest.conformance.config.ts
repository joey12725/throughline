import { defineConfig } from "vitest/config";

// Conformance run of the lens engine against a live Throughline API (never part of
// `pnpm test`): profiles every channel and executes every statement every candidate
// lens produces.
//
//   . <env file exporting THROUGHLINE_URL and THROUGHLINE_TOKEN>
//   pnpm exec vitest run --config vitest.conformance.config.ts
export default defineConfig({
  resolve: { alias: { "@": new URL("./src", import.meta.url).pathname } },
  test: {
    environment: "node",
    include: ["src/lens/conformance.ts"],
    testTimeout: 30 * 60 * 1000,
    hookTimeout: 5 * 60 * 1000,
    fileParallelism: false,
  },
});
