import { defineConfig } from "vite";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

// This config lives in frontend/ so `vite`, `react`, `react-dom` and `agentation`
// all resolve from frontend/node_modules. It serves the sibling demo-dashboard folder.
const here = dirname(fileURLToPath(import.meta.url));
const dashboardRoot = resolve(here, "../demo-dashboard");
const mods = resolve(here, "node_modules");

export default defineConfig({
  root: dashboardRoot,
  server: { port: 4178, open: false, fs: { allow: [dashboardRoot, here] } },
  resolve: {
    // Dashboard folder has no node_modules of its own; resolve deps from frontend's.
    alias: {
      react: resolve(mods, "react"),
      "react-dom/client": resolve(mods, "react-dom/client"),
      "react-dom": resolve(mods, "react-dom"),
      agentation: resolve(mods, "agentation"),
    },
  },
  optimizeDeps: {
    include: ["react", "react-dom", "react-dom/client", "agentation"],
  },
});
