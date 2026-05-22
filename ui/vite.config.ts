import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { resolve } from "path";

// Zendesk apps load the bundle from `assets/iframe.html`. The build
// emits a single `iframe.html` plus hashed JS/CSS chunks into the
// `assets/` directory the manifest expects.
export default defineConfig({
  plugins: [react()],
  root: ".",
  base: "./", // relative paths — the iframe is loaded from a Zendesk-controlled origin
  build: {
    outDir: "assets",
    emptyOutDir: false, // keep logo/icon files committed under assets/
    rollupOptions: {
      input: resolve(__dirname, "iframe.html"),
      output: {
        entryFileNames: "app-[hash].js",
        chunkFileNames: "chunk-[hash].js",
        assetFileNames: "asset-[hash][extname]",
      },
    },
    target: "es2019", // ZAF is fine with modern JS; this matches Zendesk's supported browsers.
    sourcemap: true,
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/__tests__/setup.ts"],
  },
});
